"""Assist: "añade huevos, jamón y pan a Mercadona", with doubts asked in the same chat.

- One sentence trigger adds a list of products. Clear ones go straight to the cart;
  for doubtful ones the answer ends with a question, which keeps the conversation open
  (Home Assistant continues a conversation whose last answer ends with "?").
- Answer sentences ("el segundo", "los dos", "la de Hacendado", "más opciones",
  "ninguno") are specific on purpose: a trigger always consumes the sentence, so a
  catch-all would swallow unrelated commands.
- A doubt not answered in PENDING_TTL is sent to Telegram (event mercadona_unresolved),
  like doubts from any other source.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .api import DespensaError
from .const import EVENT_UNRESOLVED

_LOGGER = logging.getLogger(__name__)

SOURCE = "voice"
OPTIONS_SPOKEN = 3
PENDING_TTL = 300  # seconds before an unanswered doubt goes to Telegram

_VERB = "(añade|añadir|agrega|agregar|apunta|apuntar|pon|poner|mete|meter)"
_LIST = "[la] [lista [de la compra] [de]] mercadona"
ADD_SENTENCES = [
    f"{_VERB} {{items}} (a|en) {_LIST}",
    f"{_VERB} (a|en) {_LIST} {{items}}",
]
_ORD = ("(primero|primera|segundo|segunda|tercero|tercera|cuarto|cuarta|quinto|quinta|"
        "uno|una|dos|tres|cuatro|cinco|1|2|3|4|5)")
ANSWER_SENTENCES = [
    f"[el|la|lo] [número] {_ORD}",
    "(los|las) (dos|tres|cuatro|cinco)",
    "(todos|todas) [ellos|ellas]",
    "(el|la|los|las) de {brand}",
    "(ninguno|ninguna|nada|déjalo|olvídalo|cancela|cancelar|ninguna de ellas)",
    "[dame|dime] (más|otras|otros) [opciones]",
    "(otra|otro|otras|otros)",
    "(sí|si|vale|venga|ok|okey) [añádelo|añádela|ese|esa|por favor]",
]

_ORDINALS = {
    "primero": 1, "primera": 1, "uno": 1, "una": 1, "1": 1,
    "segundo": 2, "segunda": 2, "dos": 2, "2": 2,
    "tercero": 3, "tercera": 3, "tres": 3, "3": 3,
    "cuarto": 4, "cuarta": 4, "cuatro": 4, "4": 4,
    "quinto": 5, "quinta": 5, "cinco": 5, "5": 5,
}
_SPOKEN_NUM = {1: "uno", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


_PREFIX = re.compile(
    r"^\s*(añade|añadir|agrega|agregar|apunta|apuntar|pon|poner|mete|meter)\s+", re.I)
_SUFFIX = re.compile(
    r"\s+(a|en)\s+(la\s+)?(lista\s+(de\s+la\s+compra\s+)?(de\s+)?)?mercadona[\s.!]*$", re.I)
_INFIX = re.compile(
    r"^(a|en)\s+(la\s+)?(lista\s+(de\s+la\s+compra\s+)?(de\s+)?)?mercadona[\s,:]+", re.I)


def items_text(raw: str, fallback: str) -> str:
    """Product part of the ORIGINAL sentence (HA strips commas before matching, the
    original text keeps them)."""
    text = _PREFIX.sub("", raw.strip(), count=1)
    stripped = _SUFFIX.sub("", text, count=1)
    if stripped == text:
        stripped = _INFIX.sub("", text, count=1)
    return stripped.strip() or fallback


def split_items(text: str) -> list[str]:
    """'huevos, jamón serrano y pan' -> ['huevos', 'jamón serrano', 'pan']."""
    parts = re.split(r",|;|\s+y\s+|\s+e\s+", text.strip(), flags=re.IGNORECASE)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def _label(c: dict[str, Any]) -> str:
    """Spoken option: 'Lonchas extrafinas de Incarlopsa (120 g)'."""
    if c.get("short"):
        brand = f" de {c['brand']}" if c.get("brand") else ""
        size = f" ({c['size_short']})" if c.get("size_short") else ""
        return f"{c['short']}{brand}{size}"
    size = f" ({c['size']})" if c.get("size") else ""
    return f"{c.get('name', c.get('id'))}{size}"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


@dataclass
class Doubt:
    request_id: str
    query: str
    candidates: list[dict[str, Any]]
    more: bool
    offset: int = 0  # first candidate currently offered
    created: float = field(default_factory=time.monotonic)

    def offered(self) -> list[dict[str, Any]]:
        return self.candidates[self.offset:self.offset + OPTIONS_SPOKEN]


@dataclass
class Session:
    doubts: list[Doubt] = field(default_factory=list)
    timer: CALLBACK_TYPE | None = None
    updated: float = field(default_factory=time.monotonic)


class AssistHandler:
    def __init__(self, hass: HomeAssistant, get_coordinator: Any) -> None:
        self.hass = hass
        self._get_coordinator = get_coordinator
        self.sessions: dict[str, Session] = {}
        self._unsub: list[CALLBACK_TYPE] = []

    # --- registration ----------------------------------------------------------------

    def register(self) -> None:
        self._unsub.append(_register_trigger(self.hass, ADD_SENTENCES, self._on_add))
        self._unsub.append(_register_trigger(self.hass, ANSWER_SENTENCES, self._on_answer))

    def unregister(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        for s in self.sessions.values():
            if s.timer:
                s.timer()
        self.sessions.clear()

    # --- sessions --------------------------------------------------------------------

    def _key(self, user_input: Any) -> str:
        return user_input.conversation_id or "_"

    def _session_for(self, user_input: Any) -> Session | None:
        s = self.sessions.get(self._key(user_input))
        if s and s.doubts:
            return s
        # Clients that do not keep the conversation id: use the most recent open
        # session if it is fresh.
        fresh = [x for x in self.sessions.values()
                 if x.doubts and time.monotonic() - x.updated < PENDING_TTL]
        return max(fresh, key=lambda x: x.updated) if fresh else None

    def _arm_timer(self, key: str, session: Session) -> None:
        if session.timer:
            session.timer()
        session.updated = time.monotonic()

        @callback
        def _expire(_now: Any) -> None:
            s = self.sessions.pop(key, None)
            if not s:
                return
            for d in s.doubts:
                self.hass.bus.async_fire(EVENT_UNRESOLVED, {
                    "query": d.query, "reason": "sin respuesta en Assist",
                    "candidates": d.candidates[:5], "request_id": d.request_id,
                    "more": d.more or len(d.candidates) > 5, "source": SOURCE,
                    "is_text": True, "qty": 1, "mode": "ensure",
                })

        session.timer = async_call_later(self.hass, PENDING_TTL, _expire)

    # --- add -------------------------------------------------------------------------

    async def _on_add(self, user_input: Any, result: Any) -> str:
        entity = result.entities.get("items") if hasattr(result, "entities") else None
        wildcard = str(entity.text if entity is not None else "")
        coordinator = self._get_coordinator()
        if coordinator is None:
            return "La integración de Mercadona no está disponible."
        items: list[str] = []
        for chunk in split_items(items_text(user_input.text, wildcard)):
            if len(chunk.split()) > 1:
                try:
                    items.extend(await coordinator.client.segment(chunk))
                except DespensaError:
                    items.append(chunk)
            else:
                items.append(chunk)
        if not items:
            return "No he entendido qué añadir."
        added: list[str] = []
        present: list[str] = []
        missing: list[str] = []
        queued: list[str] = []
        doubts: list[Doubt] = []
        for item in items:
            try:
                r = await coordinator.client.add_item(text=item, qty=1, mode="ensure",
                                                      source=SOURCE)
            except DespensaError as exc:
                _LOGGER.warning("Despensa no responde: %s", exc)
                return "El servicio de la lista de la compra no responde ahora mismo."
            res = r.get("result")
            name = (r.get("product") or {}).get("name") or item
            if res in ("added", "incremented", "dry_run"):
                added.append(name)
            elif res == "already_present":
                present.append(name)
            elif res == "queued":
                queued.append(name)
            elif res == "unresolved" and r.get("candidates") and r.get("request_id"):
                doubts.append(Doubt(r["request_id"], item, list(r["candidates"]),
                                    bool(r.get("more"))))
            else:
                missing.append(item)
        await coordinator.async_request_refresh()
        parts: list[str] = []
        if added:
            parts.append(f"He añadido {_join(added)}.")
        if present:
            parts.append(f"{_join(present)} ya {'estaba' if len(present) == 1 else 'estaban'}"
                         " en la lista.")
        if queued:
            parts.append(f"{_join(queued)} se añadirá en cuanto Mercadona responda.")
        if missing:
            parts.append(f"No encuentro {_join(missing)} en Mercadona.")
        if doubts:
            key = self._key(user_input)
            session = self.sessions.setdefault(key, Session())
            session.doubts.extend(doubts)
            self._arm_timer(key, session)
            parts.append(self._question(session.doubts[0]))
        return " ".join(parts) or "No he añadido nada."

    @staticmethod
    def _question(d: Doubt) -> str:
        offered = d.offered()
        if len(offered) == 1:
            return f"Para {d.query} me queda: uno, {_label(offered[0])}. ¿Lo añado?"
        opts = "; ".join(f"{_SPOKEN_NUM[i]}, {_label(c)}" for i, c in enumerate(offered, 1))
        has_more = d.more or len(d.candidates) > d.offset + OPTIONS_SPOKEN
        extra = " Puedes pedirme más opciones." if has_more else ""
        return f"Para {d.query} tengo: {opts}.{extra} ¿Cuál?"

    # --- answers ---------------------------------------------------------------------

    async def _on_answer(self, user_input: Any, result: Any) -> str:
        session = self._session_for(user_input)
        if session is None:
            return "No tenía ninguna pregunta pendiente sobre la compra."
        key = next(k for k, v in self.sessions.items() if v is session)
        d = session.doubts[0]
        text = _norm(user_input.text)
        words = text.split()
        coordinator = self._get_coordinator()
        if coordinator is None:
            return "La integración de Mercadona no está disponible."

        if words and words[0] in {"ninguno", "ninguna", "nada", "dejalo", "olvidalo",
                                  "cancela", "cancelar"}:
            session.doubts.pop(0)
            return self._next(key, session, f"Vale, no añado {d.query}.")

        if any(w in {"mas", "otras", "otros", "otra", "otro"} for w in words) \
                and not any(w in _ORDINALS for w in words):
            return await self._more(key, session, d, coordinator)

        chosen: list[dict[str, Any]] = []
        offered = d.offered()
        if words and words[0] in {"todos", "todas"}:
            chosen = offered
        elif len(words) >= 2 and words[0] in {"los", "las"} and words[1] in _ORDINALS:
            chosen = offered[:_ORDINALS[words[1]]]
        elif "de" in words and words.index("de") <= 1:
            brand = words[words.index("de") + 1:]
            chosen = [c for c in offered
                      if brand and all(b in _norm(_label(c)).split() for b in brand)][:1]
            if not chosen:
                return (f"No veo ninguna de {' '.join(brand)} entre las opciones. "
                        + self._question(d))
        elif words and words[0] in {"si", "vale", "venga", "ok", "okey"}:
            if len(offered) != 1:
                return "Dime cuál: " + self._question(d)
            chosen = offered
        else:
            n = next((_ORDINALS[w] for w in words if w in _ORDINALS), None)
            if n is None or n > len(offered):
                return "No he entendido cuál. " + self._question(d)
            chosen = [offered[n - 1]]

        names: list[str] = []
        for i, c in enumerate(chosen):
            try:
                if i == 0:
                    r = await coordinator.client.choose(d.request_id, c["id"])
                else:
                    r = await coordinator.client.add_item(product_id=c["id"], qty=1,
                                                          mode="ensure", source=SOURCE)
            except DespensaError as exc:
                _LOGGER.warning("Despensa no responde: %s", exc)
                return "El servicio de la lista de la compra no responde ahora mismo."
            if r.get("result") in ("added", "incremented", "already_present",
                                   "already_chosen", "dry_run", "queued"):
                names.append(c.get("name", c["id"]))
        await coordinator.async_request_refresh()
        session.doubts.pop(0)
        done = f"Añadido {_join(names)}." if names else "No he podido añadirlo."
        return self._next(key, session, done)

    async def _more(self, key: str, session: Session, d: Doubt, coordinator: Any) -> str:
        if len(d.candidates) > d.offset + OPTIONS_SPOKEN:
            d.offset += OPTIONS_SPOKEN
        elif d.more:
            try:
                page = await coordinator.client.more(d.request_id)
            except DespensaError:
                return "No puedo traer más opciones ahora. " + self._question(d)
            new = [c for c in page.get("candidates", [])
                   if c["id"] not in {x["id"] for x in d.candidates}]
            d.more = bool(page.get("more"))
            if not new:
                return "No hay más opciones. " + self._question(d)
            d.candidates.extend(new)
            d.offset = len(d.candidates) - len(new)
        else:
            return "No hay más opciones. " + self._question(d)
        self._arm_timer(key, session)
        return self._question(d)

    def _next(self, key: str, session: Session, prefix: str) -> str:
        if session.doubts:
            self._arm_timer(key, session)
            return f"{prefix} {self._question(session.doubts[0])}"
        if session.timer:
            session.timer()
        self.sessions.pop(key, None)
        return prefix


def _register_trigger(hass: HomeAssistant, sentences: list[str], cb: Any) -> CALLBACK_TYPE:
    """Register sentence triggers on the default conversation agent.

    Home Assistant changed this internal API: up to 2026.2 it takes a TriggerDetails
    object; from 2026.10 (at least) it takes the sentences and the callback.
    """
    from homeassistant.components.conversation.agent_manager import get_agent_manager

    manager = get_agent_manager(hass)
    fn = manager.register_trigger
    # Count positional parameters without inspect.signature: on Python 3.14 that
    # evaluates annotations (PEP 649) and fails on names that only exist under
    # TYPE_CHECKING in Home Assistant (seen on HA 2026.10.4).
    code = getattr(getattr(fn, "__func__", fn), "__code__", None)
    n_params = code.co_argcount - 1 if code is not None else 2  # minus self
    if n_params >= 2:
        try:
            unsub: CALLBACK_TYPE = fn(sentences, cb)
            return unsub
        except TypeError:
            _LOGGER.debug("register_trigger(sentences, callback) no aceptado; pruebo la "
                          "forma antigua")
    from homeassistant.components.conversation.trigger import TriggerDetails

    unsub = fn(TriggerDetails(sentences, cb))
    return unsub

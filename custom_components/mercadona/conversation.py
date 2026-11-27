"""Assist: add, set quantities and remove products by voice, doubts in the same chat.

    añade huevos, 2 jamón serrano y pan a Mercadona     (2 = "que haya al menos 2")
    añade 2 más de jamón a Mercadona                    (suma 2)
    pon 3 leches en Mercadona                           (deja exactamente 3)
    quita el jamón de Mercadona                         (busca en lo que hay en la lista)

- Clear products go straight to the cart. For doubtful ones the answer is a short
  question: "Jamón serrano: ¿gran reserva de Hacendado o lonchas extrafinas de
  Incarlopsa?". Answers: "el segundo", "el de Hacendado", "el gran reserva", "los dos",
  "otras", "sí" (one option), "ninguno", or the option itself as offered, with or
  without article or brand ("lonchas extrafinas", "gran reserva de Hacendado").
- Answer sentences exist only while a question is pending, so they never take over
  other Assist commands: besides the fixed ones, any sentence that contains a word of
  the options being offered (built from them, re-registered when they change). A
  sentence with none of those words is not an answer and keeps reaching Home Assistant.
  An answer from another conversation counts only if it comes from the same satellite,
  device or user.
- A doubt not answered in PENDING_TTL goes to Telegram (event mercadona_unresolved).
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

from .api import DespensaError, DespensaNotSupported
from .const import EVENT_UNRESOLVED

_LOGGER = logging.getLogger(__name__)

SOURCE = "voice"
OPTIONS_SPOKEN = 3
PENDING_TTL = 300  # seconds before an unanswered doubt goes to Telegram

# Verbs as said, with and without accents (speech recognition writes either). Multi-word
# ones first: the regex that strips them from the sentence tries them in this order.
ADD_VERBS = (
    "hay que comprar", "hace falta", "hacen falta", "añádeme", "añademe", "añade",
    "añadir", "agrégame", "agregame", "agrega", "agregar", "apúntame", "apuntame",
    "apunta", "apuntar", "anótame", "anotame", "anota", "anotar", "ponme", "pon", "poner",
    "méteme", "meteme", "mete", "meter", "incluye", "incluir", "compra", "comprar",
    "necesito", "necesitamos", "falta", "faltan", "quiero", "queremos",
)
DEL_VERBS = (
    "ya no hace falta", "quítame", "quitame", "quita", "quitar", "bórrame", "borrame",
    "borra", "borrar", "elimina", "eliminar", "saca", "sacar", "tacha", "tachar",
)
_LIST = "[el|la] [(lista|carrito|cesta) [de la compra] [de|del]] mercadona"
_POLITE = "[por favor|oye]"
_ADD_VERB = f"({'|'.join(ADD_VERBS)})"
_DEL_VERB = f"({'|'.join(DEL_VERBS)})"
# "Mercadona" anywhere: at the end, after the verb or before it.
ADD_SENTENCES = [
    f"{_POLITE} {_ADD_VERB} {{items}} (a|en|para|de|del) {_LIST} [por favor]",
    f"{_POLITE} {_ADD_VERB} (a|en|para) {_LIST} {{items}}",
    f"{_POLITE} [a|en|para] {_LIST} {_ADD_VERB} {{items}}",
]
REMOVE_SENTENCES = [
    f"{_POLITE} {_DEL_VERB} {{items}} (de|del|en) {_LIST} [por favor]",
    f"{_POLITE} {_DEL_VERB} (de|del|en) {_LIST} {{items}}",
    f"{_POLITE} [de|del|en] {_LIST} {_DEL_VERB} {{items}}",
]
SET_SENTENCES = [
    f"(cambia|cambiar) {{items}} a {{qty}} [unidades] (en|de) {_LIST}",
]
_ORD = ("(primero|primera|segundo|segunda|tercero|tercera|cuarto|cuarta|quinto|quinta|"
        "uno|una|dos|tres|cuatro|cinco|1|2|3|4|5)")
ANSWER_SENTENCES = [
    f"[el|la|lo] [número] {_ORD}",
    "(los|las) (dos|tres|cuatro|cinco)",
    "(todos|todas) [ellos|ellas]",
    "(el|la|los|las) de {brand}",
    "(el|la|los|las) {option}",
    "(ninguno|ninguna|nada|déjalo|olvídalo|cancela|cancelar|ninguna de ellas)",
    "[dame|dime] (más|otras|otros) [opciones]",
    "(otra|otro|otras|otros)",
    "(sí|si|vale|venga|ok|okey) [añádelo|añádela|quítalo|quítala|ese|esa|por favor]",
]

_ORDINALS = {
    "primero": 1, "primera": 1, "uno": 1, "una": 1, "1": 1,
    "segundo": 2, "segunda": 2, "dos": 2, "2": 2,
    "tercero": 3, "tercera": 3, "tres": 3, "3": 3,
    "cuarto": 4, "cuarta": 4, "cuatro": 4, "4": 4,
    "quinto": 5, "quinta": 5, "cinco": 5, "5": 5,
}
_NUMBERS = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "media docena": 6, "una docena": 12, "docena": 12,
}
_ARTICLES = {"el", "la", "los", "las", "lo"}
_STOP = {"de", "del", "la", "el", "los", "las", "con", "sin", "y", "en", "a", "al"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


# --- parsing ---------------------------------------------------------------------------

_LIST_RE = (r"(?:(?:el|la)\s+)?(?:(?:lista|carrito|cesta)\s+(?:de\s+la\s+compra\s+)?"
            r"(?:(?:de|del)\s+)?)?mercadona")
_VERB_RE = "|".join(re.escape(v) for v in (*ADD_VERBS, *DEL_VERBS, "cambia", "cambiar"))
_POLITE_RE = re.compile(r"^(?:por\s+favor|oye)[\s,]+|[\s,]+por\s+favor$", re.I)
_LEAD_LIST = re.compile(rf"^(?:(?:a|en|para|de|del)\s+)?{_LIST_RE}[\s,:]+", re.I)
_PREFIX = re.compile(rf"^(?:{_VERB_RE})\b[\s,:]*", re.I)
_INFIX = re.compile(rf"^(?:a|en|para|de|del)\s+{_LIST_RE}[\s,:]+", re.I)
_SUFFIX = re.compile(rf"[\s,]+(?:a|en|para|de|del)\s+{_LIST_RE}$", re.I)
_QTY = re.compile(
    r"^(?P<n>\d+(?:[.,]\d+)?|media docena|una docena|docena|un|uno|una|dos|tres|cuatro|"
    r"cinco|seis|siete|ocho|nueve|diez|once|doce)\s+(?P<more>m[aá]s\s+)?(?:(?:de|del)\s+)?"
    r"(?P<rest>.+)$", re.I)


def items_text(raw: str, fallback: str) -> str:
    """Product part of the ORIGINAL sentence (HA strips commas before matching, the
    original text keeps them): without "por favor", the verb and "(en la lista de)
    Mercadona", wherever it was said."""
    text = raw.strip().strip(".!?¡¿ ")
    text = _POLITE_RE.sub("", _POLITE_RE.sub("", text, count=1), count=1).strip()
    text = _LEAD_LIST.sub("", text, count=1)
    text = _PREFIX.sub("", text, count=1)
    stripped = _INFIX.sub("", text, count=1)
    if stripped == text:
        stripped = _SUFFIX.sub("", text, count=1)
    return stripped.strip(" ,.:") or fallback


def split_items(text: str) -> list[str]:
    """'huevos, jamón serrano y pan' -> ['huevos', 'jamón serrano', 'pan']."""
    parts = re.split(r",|;|\s+y\s+|\s+e\s+", text.strip(), flags=re.IGNORECASE)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def _to_number(word: str) -> float | None:
    w = word.lower().strip()
    if w in _NUMBERS:
        return float(_NUMBERS[w])
    try:
        return float(w.replace(",", "."))
    except ValueError:
        return None


@dataclass
class Item:
    text: str
    qty: float = 1.0
    more: bool = False      # "2 más de" -> add on top
    explicit: bool = False  # a number was said


def parse_item(chunk: str) -> Item:
    """'2 jamón serrano' -> Item('jamón serrano', 2); '2 más de jamón' -> add;
    'una docena de huevos' -> 12; 'el jamón' -> 'jamón'."""
    words = chunk.split()
    while words and words[0].lower() in _ARTICLES:
        words.pop(0)
    chunk = " ".join(words)
    m = _QTY.match(chunk)
    if m:
        n = _to_number(m["n"])
        if n is not None and 0 < n <= 50:
            explicit = m["n"].lower() not in {"un", "uno", "una"} or bool(m["more"])
            return Item(m["rest"].strip(), n, bool(m["more"]), explicit)
    return Item(chunk)


# --- presentation ----------------------------------------------------------------------

def _option(c: dict[str, Any], with_size: bool = False) -> str:
    """Spoken option: 'gran reserva de Hacendado' (+ size only when needed)."""
    if c.get("short"):
        text = c["short"][:1].lower() + c["short"][1:]
        # The short name falls back to the full name, which may already carry the
        # brand ("Caldo de pollo Hacendado"): never "... Hacendado de Hacendado".
        if c.get("brand") and _norm(c["brand"]) not in _norm(text):
            text += f" de {c['brand']}"
    else:
        text = c.get("name") or str(c.get("id"))
    if with_size and c.get("size_short"):
        text += f" ({c['size_short']})"
    return text


def _options(cands: list[dict[str, Any]]) -> list[str]:
    plain = [_option(c) for c in cands]
    dupes = {p for p in plain if plain.count(p) > 1}
    return [_option(c, with_size=p in dupes) for c, p in zip(cands, plain, strict=True)]


def _or(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    last = items[-1]
    conj = "u" if _norm(last)[:1] == "o" else "o"
    return ", ".join(items[:-1]) + f" {conj} {last}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def _qty_text(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else str(q).replace(".", ",")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _failed_text(failed: list[str], verb: str, everything: bool) -> str:
    if everything:
        return "El servicio de la lista de la compra no responde ahora mismo."
    return f"No he podido {verb} {_and(failed)}: el servicio no responde ahora mismo."


@dataclass
class Doubt:
    request_id: str
    query: str
    candidates: list[dict[str, Any]]
    more: bool
    action: str = "add"  # add | remove
    qty: float = 1.0
    offset: int = 0
    created: float = field(default_factory=time.monotonic)

    def offered(self) -> list[dict[str, Any]]:
        return self.candidates[self.offset:self.offset + OPTIONS_SPOKEN]

    def has_more(self) -> bool:
        return self.more or len(self.candidates) > self.offset + OPTIONS_SPOKEN


@dataclass
class Session:
    doubts: list[Doubt] = field(default_factory=list)
    timer: CALLBACK_TYPE | None = None
    updated: float = field(default_factory=time.monotonic)
    origin: str | None = None  # satellite / device / user that asked


def _origin(user_input: Any) -> str | None:
    """Who is talking: the satellite or device, else the HA user."""
    for attr in ("satellite_id", "device_id"):
        value = getattr(user_input, attr, None)
        if value:
            return f"{attr}:{value}"
    user = getattr(getattr(user_input, "context", None), "user_id", None)
    return f"user:{user}" if user else None


_WORD = re.compile(r"[^\W\d_]+")
_VERBS = {_norm(v).split()[0] for v in (*ADD_VERBS, *DEL_VERBS, "cambia", "cambiar")}


def _label(c: dict[str, Any]) -> str:
    return f"{c.get('short', '')} {c.get('brand') or ''} {c.get('name', '')}"


def option_words(cands: list[dict[str, Any]]) -> list[str]:
    """Words of the options offered, as said and without accents ('ángel', 'angel')."""
    words: set[str] = set()
    for c in cands:
        for w in _WORD.findall(_label(c).lower()):
            if len(w) < 3 or _norm(w) in _STOP:
                continue
            words.add(w)
            words.add(_norm(w))
    return sorted(words)


def answer_sentences(doubts: list[Doubt]) -> list[str]:
    """Fixed answer sentences plus "<anything> <word of an option> <anything>"."""
    words: set[str] = set()
    for d in doubts:
        words.update(option_words(d.offered()))
    if not words:
        return list(ANSWER_SENTENCES)
    return [*ANSWER_SENTENCES, f"[{{pre}}] ({'|'.join(sorted(words))}) [{{post}}]"]


def question(d: Doubt) -> str:
    opts = _options(d.offered())
    if d.has_more() and len(opts) > 1:
        opts = [*opts, "otra"]
    what = f"{_qty_text(d.qty)} de {d.query}" if d.qty != 1 else d.query
    if d.action == "remove":
        if len(opts) == 1:
            return f"De {d.query} solo veo {opts[0]}. ¿Lo quito?"
        return f"¿Qué {d.query} quito: {_or(opts)}?"
    if len(opts) == 1:
        return f"{_cap(what)}: ¿{opts[0]}?"
    return f"{_cap(what)}: ¿{_or(opts)}?"


# --- handler ---------------------------------------------------------------------------

class AssistHandler:
    def __init__(self, hass: HomeAssistant, get_coordinator: Any) -> None:
        self.hass = hass
        self._get_coordinator = get_coordinator
        self.sessions: dict[str, Session] = {}
        self._unsub: list[CALLBACK_TYPE] = []
        self._answers_unsub: CALLBACK_TYPE | None = None
        self._answers_sentences: list[str] = []
        self.closed: set[str] = set()  # request ids the cart answered

    # registration

    def register(self) -> None:
        coordinator = self._coord()
        if coordinator is not None:
            self._unsub.append(coordinator.async_add_listener(self._on_cart_update))
        self._unsub.append(_register_trigger(self.hass, ADD_SENTENCES, self._on_add))
        self._unsub.append(_register_trigger(self.hass, REMOVE_SENTENCES, self._on_remove))
        self._unsub.append(_register_trigger(self.hass, SET_SENTENCES, self._on_set))

    def unregister(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        for s in self.sessions.values():
            if s.timer:
                s.timer()
        self.sessions.clear()
        self._sync_answers()

    def _sync_answers(self) -> None:
        """Answer sentences for the questions being asked now (first doubt of each
        session); registered only while there is one, rebuilt when the options change."""
        current = [s.doubts[0] for s in self.sessions.values() if s.doubts]
        wanted = answer_sentences(current) if current else []
        if wanted == self._answers_sentences and (self._answers_unsub is not None) == bool(wanted):
            return
        if self._answers_unsub is not None:
            self._answers_unsub()
            self._answers_unsub = None
        self._answers_sentences = wanted
        if wanted:
            self._answers_unsub = _register_trigger(self.hass, wanted, self._on_answer)

    # sessions

    def _key(self, user_input: Any) -> str:
        return user_input.conversation_id or "_"

    def _session_for(self, user_input: Any) -> Session | None:
        s = self.sessions.get(self._key(user_input))
        if s and s.doubts:
            return s
        origin = _origin(user_input)
        if origin is None:
            return None
        fresh = [x for x in self.sessions.values()
                 if x.doubts and x.origin == origin
                 and time.monotonic() - x.updated < PENDING_TTL]
        return max(fresh, key=lambda x: x.updated) if fresh else None

    def _arm_timer(self, key: str, session: Session) -> None:
        if session.timer:
            session.timer()
        session.updated = time.monotonic()

        @callback
        def _expire(_now: Any) -> None:
            s = self.sessions.pop(key, None)
            self._sync_answers()
            if s and s.doubts:
                self.hass.async_create_task(self._to_telegram(s.doubts))

        session.timer = async_call_later(self.hass, PENDING_TTL, _expire)

    async def _to_telegram(self, doubts: list[Doubt]) -> None:
        """Unanswered doubts go to Telegram, except those the cart already answered
        (the product was added by hand in the app meanwhile): read the cart first."""
        coordinator = self._coord()
        if coordinator is not None:
            try:
                await coordinator.async_refresh()
            except Exception:  # noqa: BLE001 - Telegram is still better than nothing
                _LOGGER.debug("No se pudo releer el carrito antes de Telegram")
        for d in doubts:
            if d.request_id in self.closed:
                _LOGGER.debug("Duda %s resuelta en el carrito: no va a Telegram",
                              d.request_id)
                continue
            self.hass.bus.async_fire(EVENT_UNRESOLVED, {
                "query": d.query, "reason": "sin respuesta en Assist",
                "candidates": d.candidates[:5], "request_id": d.request_id,
                "more": d.more or len(d.candidates) > 5, "source": SOURCE,
                "is_text": True, "qty": d.qty, "mode": "ensure", "action": d.action,
            })

    @callback
    def _on_cart_update(self) -> None:
        """The service closes the questions the cart answered: forget them here too."""
        coordinator = self._coord()
        cart = (coordinator.data or {}).get("cart") if coordinator is not None else None
        closed = set((cart or {}).get("closed_requests") or [])
        if not closed:
            return
        self.closed |= closed
        for key, session in list(self.sessions.items()):
            session.doubts = [d for d in session.doubts if d.request_id not in closed]
            if not session.doubts:
                if session.timer:
                    session.timer()
                self.sessions.pop(key, None)
        self._sync_answers()

    def _open(self, user_input: Any, doubts: list[Doubt]) -> str:
        key = self._key(user_input)
        session = self.sessions.setdefault(key, Session())
        session.origin = _origin(user_input)
        session.doubts.extend(doubts)
        self._arm_timer(key, session)
        self._sync_answers()
        return question(session.doubts[0])

    def _coord(self) -> Any:
        return self._get_coordinator()

    async def _items(self, user_input: Any, result: Any) -> list[Item]:
        entity = result.entities.get("items") if hasattr(result, "entities") else None
        wildcard = str(entity.text if entity is not None else "")
        parts = [p for p in (parse_item(c) for c in split_items(
            items_text(user_input.text, wildcard))) if p.text]
        if not parts:
            return []
        client = self._coord().client
        try:
            # The service joins what belongs together ("caldo de pollo, gallina blanca":
            # a comma at a pause, then a brand) and splits runs without separators.
            pieces = await client.segment_parts([p.text for p in parts])
        except DespensaNotSupported:
            pieces = await self._segment_each(parts)
        except DespensaError:
            pieces = [(p.text, i) for i, p in enumerate(parts)]
        out: list[Item] = []
        seen: set[int] = set()
        for text, idx in pieces:
            part = parts[idx] if 0 <= idx < len(parts) else Item(text)
            # The quantity said belongs to the first product of its part.
            out.append(Item(text, part.qty, part.more, part.explicit) if idx not in seen
                       else Item(text))
            seen.add(idx)
        return out

    async def _segment_each(self, parts: list[Item]) -> list[tuple[str, int]]:
        """Older service without POST /v1/segment: split each part on its own."""
        out: list[tuple[str, int]] = []
        for i, p in enumerate(parts):
            pieces = [p.text]
            if len(p.text.split()) > 1:
                try:
                    pieces = await self._coord().client.segment(p.text)
                except DespensaError:
                    pass
            out.extend((piece, i) for piece in pieces)
        return out

    # add / set

    async def _on_add(self, user_input: Any, result: Any) -> str:
        if self._coord() is None:
            return "La integración de Mercadona no está disponible."
        items = await self._items(user_input, result)
        if not items:
            return "No he entendido qué añadir."
        exact = user_input.text.strip().lower().startswith(("pon ", "poner "))
        return await self._apply(user_input, items, exact=exact)

    async def _on_set(self, user_input: Any, result: Any) -> str:
        if self._coord() is None:
            return "La integración de Mercadona no está disponible."
        entity = result.entities.get("qty") if hasattr(result, "entities") else None
        qty = _to_number(str(entity.text if entity is not None else ""))
        item_entity = result.entities.get("items") if hasattr(result, "entities") else None
        text = parse_item(str(item_entity.text if item_entity is not None else "")).text
        if not qty or not text or qty > 50:
            return "No he entendido la cantidad."
        return await self._apply(user_input, [Item(text, qty, False, True)], exact=True)

    async def _apply(self, user_input: Any, items: list[Item], *, exact: bool) -> str:
        coordinator = self._coord()
        added: list[str] = []
        updated: list[str] = []
        present: list[str] = []
        missing: list[str] = []
        queued: list[str] = []
        failed: list[str] = []
        doubts: list[Doubt] = []
        for item in items:
            if item.more:
                mode = "add"
            elif exact and item.explicit:
                mode = "set"
            else:
                mode = "ensure"
            try:
                r = await coordinator.client.add_item(text=item.text, qty=item.qty,
                                                      mode=mode, source=SOURCE)
            except DespensaError as exc:
                # Keep going: what was already added stays, the doubts already created
                # in the service are still asked, and this one is said as failed.
                _LOGGER.warning("Despensa no responde con %r: %s", item.text, exc)
                failed.append(item.text)
                continue
            res = r.get("result")
            name = (r.get("product") or {}).get("name") or item.text
            label = f"{_qty_text(item.qty)} de {name}" if item.qty != 1 else name
            if res in ("added", "dry_run"):
                added.append(label)
            elif res in ("incremented", "updated"):
                updated.append(label)
            elif res == "already_present":
                present.append(name)
            elif res == "queued":
                queued.append(name)
            elif res == "unresolved" and r.get("candidates") and r.get("request_id"):
                doubts.append(Doubt(r["request_id"], item.text, list(r["candidates"]),
                                    bool(r.get("more")), "add", item.qty))
            else:
                missing.append(item.text)
        await coordinator.async_request_refresh()
        parts: list[str] = []
        if added:
            parts.append(f"He añadido {_and(added)}.")
        if updated:
            parts.append(f"Ahora hay {_and(updated)}.")
        if present:
            parts.append(f"{_cap(_and(present))} ya "
                         f"{'estaba' if len(present) == 1 else 'estaban'} en la lista.")
        if queued:
            parts.append(f"{_cap(_and(queued))} se añadirá en cuanto Mercadona responda.")
        if missing:
            parts.append(f"No encuentro {_and(missing)} en Mercadona.")
        if failed:
            parts.append(_failed_text(failed, "añadir", len(failed) == len(items)))
        if doubts:
            parts.append(self._open(user_input, doubts))
        return " ".join(parts) or "No he añadido nada."

    # remove

    async def _on_remove(self, user_input: Any, result: Any) -> str:
        coordinator = self._coord()
        if coordinator is None:
            return "La integración de Mercadona no está disponible."
        items = [parse_item(c) for c in
                 split_items(items_text(user_input.text, ""))]
        if not items:
            return "No he entendido qué quitar."
        removed: list[str] = []
        absent: list[str] = []
        failed: list[str] = []
        doubts: list[Doubt] = []
        for item in items:
            try:
                r = await coordinator.client.remove_text(item.text, SOURCE)
            except DespensaError as exc:
                _LOGGER.warning("Despensa no responde con %r: %s", item.text, exc)
                failed.append(item.text)
                continue
            res = r.get("result")
            if res in ("removed", "dry_run"):
                removed.append((r.get("product") or {}).get("name") or item.text)
            elif res == "unresolved" and r.get("candidates") and r.get("request_id"):
                doubts.append(Doubt(r["request_id"], item.text, list(r["candidates"]),
                                    bool(r.get("more")), "remove"))
            else:
                absent.append(item.text)
        await coordinator.async_request_refresh()
        parts: list[str] = []
        if removed:
            parts.append(f"He quitado {_and(removed)}.")
        if absent:
            parts.append(f"{_cap(_and(absent))} no "
                         f"{'estaba' if len(absent) == 1 else 'estaban'} en la lista.")
        if failed:
            parts.append(_failed_text(failed, "quitar", len(failed) == len(items)))
        if doubts:
            parts.append(self._open(user_input, doubts))
        return " ".join(parts) or "No he quitado nada."

    # answers

    async def _on_answer(self, user_input: Any, result: Any) -> str | None:
        words = _norm(user_input.text).split()
        if "mercadona" in words and any(w in _VERBS for w in words[:4]):
            # "añade leche de Hacendado a Mercadona" also contains an option word: Home
            # Assistant runs every matching trigger, and the add/remove one answers.
            return None
        session = self._session_for(user_input)
        if session is None:
            return "No tenía ninguna pregunta pendiente sobre la compra."
        key = next(k for k, v in self.sessions.items() if v is session)
        d = session.doubts[0]
        coordinator = self._coord()
        if coordinator is None:
            return "La integración de Mercadona no está disponible."

        if words and words[0] in {"ninguno", "ninguna", "nada", "dejalo", "olvidalo",
                                  "cancela", "cancelar"}:
            _drop(session, d)
            verb = "quito" if d.action == "remove" else "añado"
            return self._next(key, session, f"Vale, no {verb} {d.query}.")

        if any(w in {"mas", "otras", "otros", "otra", "otro"} for w in words) \
                and not any(w in _ORDINALS for w in words):
            return await self._more(key, session, d, coordinator)

        offered = d.offered()
        chosen: list[dict[str, Any]] = []
        if words and words[0] in {"todos", "todas"}:
            chosen = offered
        elif len(words) >= 2 and words[0] in {"los", "las"} and words[1] in _ORDINALS:
            chosen = offered[:_ORDINALS[words[1]]]
        elif words and words[0] in {"si", "vale", "venga", "ok", "okey"}:
            if len(offered) != 1:
                return "Dime cuál. " + question(d)
            chosen = offered
        elif any(w in _ORDINALS for w in words) and len(words) <= 3:
            n = next(_ORDINALS[w] for w in words if w in _ORDINALS)
            if n > len(offered):
                return "No he entendido cuál. " + question(d)
            chosen = [offered[n - 1]]
        else:
            said = [w for w in words if w not in _STOP]
            scored = []
            for c in offered:
                label = set(_norm(f"{c.get('short', '')} {c.get('brand') or ''} "
                                  f"{c.get('name', '')}").split())
                hits = sum(1 for w in said if any(x.startswith(w) for x in label))
                scored.append((hits, c))
            best = max((h for h, _ in scored), default=0)
            winners = [c for h, c in scored if h == best and best > 0]
            if len(winners) != 1:
                return "No he entendido cuál. " + question(d)
            chosen = winners

        names: list[str] = []
        for i, c in enumerate(chosen):
            try:
                if i == 0:
                    r = await coordinator.client.choose(d.request_id, c["id"])
                elif d.action == "remove":
                    r = await coordinator.client.remove_item(c["id"], SOURCE)
                else:
                    r = await coordinator.client.add_item(product_id=c["id"], qty=d.qty,
                                                          mode="ensure", source=SOURCE)
            except DespensaError as exc:
                _LOGGER.warning("Despensa no responde: %s", exc)
                return "El servicio de la lista de la compra no responde ahora mismo."
            if r.get("result") not in ("error", "not_present"):
                names.append(c.get("name", c["id"]))
        await coordinator.async_request_refresh()
        _drop(session, d)
        if not names:
            done = "No he podido hacerlo."
        elif d.action == "remove":
            done = f"Quitado {_and(names)}."
        else:
            qty = f"{_qty_text(d.qty)} de " if d.qty != 1 else ""
            done = f"Añadido {qty}{_and(names)}."
        return self._next(key, session, done)

    async def _more(self, key: str, session: Session, d: Doubt, coordinator: Any) -> str:
        if len(d.candidates) > d.offset + OPTIONS_SPOKEN:
            d.offset += OPTIONS_SPOKEN
        elif d.more:
            try:
                page = await coordinator.client.more(d.request_id)
            except DespensaError:
                return "No puedo traer más opciones ahora. " + question(d)
            new = [c for c in page.get("candidates", [])
                   if c["id"] not in {x["id"] for x in d.candidates}]
            d.more = bool(page.get("more"))
            if not new:
                return "No hay más opciones. " + question(d)
            d.candidates.extend(new)
            d.offset = len(d.candidates) - len(new)
        else:
            return "No hay más opciones. " + question(d)
        self._arm_timer(key, session)
        self._sync_answers()
        return question(d)

    def _next(self, key: str, session: Session, prefix: str) -> str:
        if session.doubts:
            self._arm_timer(key, session)
            return f"{prefix} {question(session.doubts[0])}"
        if session.timer:
            session.timer()
        self.sessions.pop(key, None)
        self._sync_answers()
        return prefix


def _drop(session: Session, doubt: Doubt) -> None:
    """Remove this doubt (not "the first"): another answer may have arrived meanwhile."""
    if doubt in session.doubts:
        session.doubts.remove(doubt)


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

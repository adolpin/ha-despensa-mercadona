<p align="center"><img src="custom_components/mercadona/brand/icon@2x.png" width="128" alt="Despensa"></p>

# Mercadona (Despensa) para Home Assistant

Integración de Home Assistant para un uso doméstico concreto: manejar como lista de la
compra el carrito de la tienda online de Mercadona, a través de un servicio local propio
("Despensa") que **no forma parte de este repositorio**.

La integración no habla con Mercadona ni guarda credenciales de Mercadona. Solo llama por
HTTP a la API local del servicio Despensa, con un token propio de esa API.

> Proyecto personal, no afiliado ni respaldado por Mercadona, S.A.

## Qué aporta

- `todo.mercadona`: el carrito como lista de tareas. Añadir un elemento lo busca y lo
  añade al carrito sin duplicarlo; marcarlo como hecho lo quita.
- Acción `mercadona.add_product` (texto, EAN o id de producto; modos `ensure` = que haya
  al menos esa cantidad, `add` = sumar y `set` = cantidad exacta).
- Evento `mercadona_unresolved` con candidatos cuando un texto es ambiguo, y acciones
  `mercadona.choose_candidate` y `mercadona.more_candidates` para responderlo (por
  ejemplo, desde botones de un bot de mensajería).
- Sensores de diagnóstico: estado del servicio, caducidad de la sesión, altas pendientes
  y productos en el carrito.
- Voz con Assist, con frases propias en español: "añade huevos, 2 de jamón y pan a
  Mercadona", "añade 2 más de jamón a Mercadona", "pon 3 leches en Mercadona", "quita el
  jamón de Mercadona". Si hay duda, pregunta en la misma conversación.

## Instalación

HACS, Repositorios personalizados, esta URL, categoría "Integración". Reiniciar Home
Assistant y añadir la integración "Mercadona (Despensa)" con la dirección del servicio y
su token.

## API que espera del servicio

| Método | Ruta | Uso |
|---|---|---|
| GET | `/v1/health` | estado y caducidad de la sesión |
| GET | `/v1/cart` | líneas del carrito |
| POST | `/v1/items` | alta (`text`/`ean`/`product_id`, `qty`, `mode`, `source`, `learn_text`) |
| POST | `/v1/items/remove` | baja por texto, buscando en el carrito |
| DELETE | `/v1/items/{product_id}` | baja |
| POST | `/v1/requests/{id}/choose` | elegir un candidato de una duda |
| GET | `/v1/requests/{id}/more` | más candidatos de una duda |
| GET | `/v1/segment` | trocear una frase en productos |

Todas con `Authorization: Bearer <token>`.

## Licencia

MIT.

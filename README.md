# unimilib

An asynchronous Python library for interacting with the University of Milan (UniMi) library booking portal ([prenotabiblio.sba.unimi.it](https://prenotabiblio.sba.unimi.it)).

> **Disclaimer:** This is an unofficial library and is not affiliated with or endorsed by the University of Milan (Università degli Studi di Milano) or EasyStaff.

---

## Features

- **Async-first client** built on `httpx2`
- **CAS authentication** with automatic JWT decoding to populate user details (`UnimiUser`)
- **Library & seat availability** lookup with TTL caching (`async-lru`)
- **Reservation management** — list, inspect, update, free up seats, and delete bookings
- **Safe credential handling** using `pydantic.SecretStr` so passwords are not printed in plain text
- **Optional proxy support**

---

## Requirements

- Python `>=3.12,<4.0`
- [Poetry](https://python-poetry.org/) (recommended for development)

---

## Installation

### With pip (from GitHub)

```bash
pip install git+https://github.com/flalugli/unimilib.git
```

### For local development (with Poetry)

```bash
git clone https://github.com/flalugli/unimilib.git
cd unimilib
poetry install
```

---

## Quick Start

Store your UniMi credentials in environment variables (avoid hardcoding passwords in source files):

```bash
export UNIMI_EMAIL="name.surname@studenti.unimi.it"
export UNIMI_PASSWORD="your-password"
```

```python
import asyncio
import os

from unimilib.api import LibraryUserInterface
from unimilib.models import UnimiUser


async def main() -> None:
    user = UnimiUser(
        email=os.environ["UNIMI_EMAIL"],
        password=os.environ["UNIMI_PASSWORD"],
    )
    client = LibraryUserInterface(unimi_user=user)

    try:
        await client.login()
        print(f"Logged in as: {user.fullname}")

        # List available libraries
        libraries = await client.retrieve_available_libraries()
        print("Libraries:", libraries)

        # Fetch your latest booking
        last = await client.last_booking()
        print("Last booking:", last)
    finally:
        await client.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## API Overview

### `UnimiUser` (`unimilib.models`)

Represents a UniMi user. Currently defaults to student accounts (`user_type="S"`).

| Field | Type | Populated |
| --- | --- | --- |
| `email` | `str` | Init |
| `password` | `SecretStr` | Init |
| `user_type` | `str` | Init (default: `"S"`) |
| `name` | `str \| None` | After `login()` |
| `surname` | `str \| None` | After `login()` |
| `fullname` | `str \| None` | After `login()` |
| `tax_id` | `str \| None` | After `login()` |
| `matriculation_number` | `str \| None` | After `login()` |
| `phone_number` | `str \| None` | After `login()` |

### `LibraryUserInterface` (`unimilib.api`)

Initialize with an optional `UnimiUser` and optional `proxy_url`:

```python
client = LibraryUserInterface(unimi_user=user, proxy_url="http://127.0.0.1:8080")
```

#### Authentication & Session
- `await client.login()` — Authenticates via UniMi CAS, populates `UnimiUser` fields from the JWT, and stores session cookies. Raises `MissingUserError` if no user was provided.
- `await client.validate_token() -> bool` — Checks whether the current JWT session is still valid.
- `await client.close_connection()` — Closes the underlying async HTTP client.

#### Libraries & Seat Availability
- `await client.retrieve_available_libraries() -> dict` — Returns a `{library_id: library_name}` mapping (cached for 90s).
- `await client.retrieve_services(library_id: int | None = None) -> dict` — Returns available services (optionally filtered by `library_id`, cached for 120s).
- `await client.retrieve_free_seats(library_id, service_id, time_amount=3600, days_forward=0) -> dict` — Returns available slots for a given library, service, duration (in seconds), and day offset (Europe/Rome timezone).
- `await client.retrieve_all_free_seats(library_id, time_amount=3600, days_forward=0) -> dict` — Fetches available seats across all services in a library.

#### Bookings & Reservations
- `await client.retrieve_filters() -> dict` — Returns all reservation filter options (services and areas).
- `await client.booking_info(page_number=1, sortby="data", order="desc", filters=None) -> dict` — Fetches a single page of user bookings.
- `await client.all_booking_info(sortby="data", order="desc", filters=None) -> dict` — Fetches all pages of user bookings combined under `["data"]`.
- `await client.last_booking() -> dict` — Returns the user's most recent booking.
- `await client.booking_exceptions(service_id: int) -> dict` — Returns booking exceptions for a service.
- `await client.manage_single_booking(booking_code: str) -> dict` — Retrieves management details for a specific booking code.
- `await client.single_booking_info(booking_id: str) -> int` — Returns the service ID associated with a booking code.
- `await client.free_up_seat(booking_id: str) -> dict` — Releases a booked seat (`libera_posto`).
- `await client.delete_booking(booking_id: str) -> dict` — Cancels a booking.
- `await client.update_booking_info(booking_id, new_tax_id, new_fullname, new_email) -> dict` — Updates attendee details on an existing booking.

---

## Development

Linting and formatting are configured with [Ruff](https://docs.astral.sh/ruff/) in `pyproject.toml`:

```bash
poetry run ruff check src/
poetry run ruff format src/
```

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

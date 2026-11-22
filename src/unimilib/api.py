import asyncio
import datetime
import json
import zoneinfo
from urllib.parse import quote

import httpx2
from async_lru import alru_cache
from browserforge.headers import HeaderGenerator
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from unimilib.models import MissingUserError, UnimiUser


class LibraryUserInterface:
    """Async interface for interacting with the UniMi library booking portal."""

    def __init__(self, unimi_user: UnimiUser | None = None, proxy_url: HttpUrl = None):
        """
        Parameters
        ----------
        unimi_user : UnimiUser | None, optional
            User credentials for authenticated requests, by default None.
        proxy_url : HttpUrl, optional
            Optional HTTP proxy URL for outgoing requests, by default None.
        """
        self.user = unimi_user

        # generate headers
        hgen = HeaderGenerator()
        headers = hgen.generate()
        # setup client
        self.client = httpx2.AsyncClient(
            follow_redirects=True,
            headers=headers,
            base_url="https://prenotabiblio.sba.unimi.it/portalePlanningAPI/api",
            proxy=str(proxy_url) if proxy_url is not None else None,
        )

        self.token = ""  # saved in the client memory
        self.client_id = 2  # per EasyStaff unimi è il 2 cliente

    async def close_connection(self):
        """Closes the underlying async HTTP client session."""
        await self.client.aclose()

    def _build_login_payload(self, login_page: httpx2.Response):
        """Builds the CAS login form payload from the login page HTML.

        Parameters
        ----------
        login_page : httpx2.Response
            HTTP response containing the CAS login page HTML.

        Returns
        -------
        dict
            Form payload including hidden inputs and user credentials.
        """
        soup = BeautifulSoup(login_page.text, "html.parser")
        # find the only form in the page
        form = soup.find("form")
        payload = {}

        for input_tag in form.find_all("input"):
            name = input_tag.get("name")
            value = input_tag.get("value", "")
            if name:
                payload[name] = value

        # add the necessary user data

        payload["username"] = self.user.email
        payload["selTipoUtente"] = self.user.user_type
        payload["password"] = self.user.password.get_secret_value()

        return payload

    def _extract_token_endpoint(self, login_landing: httpx2.Response):
        """Extracts the token endpoint URL and JWT from the login landing page.

        Parameters
        ----------
        login_landing : httpx2.Response
            HTTP response returned after submitting the CAS login form.

        Returns
        -------
        str
            Endpoint URL used to initialize the authenticated library session.
        """
        soup = BeautifulSoup(login_landing.text, "html.parser")
        script = soup.find("script", type="text/javascript").get_text()

        token_endpoint = script.split('"')[
            1
        ]  # the token is a string in double quotes (" ")

        # save the user token
        self.token = token_endpoint.split("/auth/me/")[1]

        return token_endpoint

    async def _refresh_cookie(self, method_name: str = "retrieve_available_libraries"):
        """Refreshes the session cookie by invoking a lightweight API method.

        Parameters
        ----------
        method_name : str, optional
            Name of the method to call to refresh the cookie, by default
            "retrieve_available_libraries".

        Raises
        ------
        AttributeError
            If the specified method name is not callable on this instance.
        """
        f = getattr(self, method_name)
        if not callable(f):
            raise AttributeError(f"Method '{method_name}' not found")
        await f()  # refresh the cookie

    async def validate_token(self) -> bool:
        """Checks whether the current JWT session token is still valid.

        Returns
        -------
        bool
            True if the token is valid, False otherwise.
        """
        # need to set a try except block
        # makes sense to execute before time expencive operations
        # to check if still logged in
        val_url = "/auth/validateJWT"
        val_check = await self.client.get(val_url)

        return val_check.json()["valid"]

    async def login(self) -> None:
        """Logs in a UnimiUser to the library platform

        Raises
        ------
        MissingUserError
            Error if no user to login into is passed
        """
        if not self.user:
            raise MissingUserError("No user passed")

        login_url = "https://cas.unimi.it/login?service=https://prenotabiblio.sba.unimi.it/auth/auth_planning.php"

        login_page = await self.client.get(login_url)

        payload = self._build_login_payload(login_page=login_page)

        login_landing = await self.client.post(login_url, data=payload)

        token_endpoint = self._extract_token_endpoint(
            login_landing=login_landing
        )  # the url that lets you login to the library user page
        # we populate the UnimiUser data
        self.user.get_data_from_jwt(self.token)
        await self.client.get(
            token_endpoint
        )  # server will release a cookie that will be stored in the client

    @alru_cache(ttl=90)  # cookie would reset after 120s but we use 30s threshold
    async def retrieve_available_libraries(self) -> dict:
        """Retrieves all available libraries from the booking portal.

        Returns
        -------
        dict
            Mapping of library IDs to library names.
        """
        endpoint = f"/aree/{self.client_id}"
        data = await self.client.get(endpoint)
        available_areas = data.json()["aree"]

        available_libs = {}
        for area in available_areas:
            id = area["id"]
            available_libs[id] = area["area_name"]

        return available_libs

    @alru_cache(ttl=120)  # max cookie age from server is 120s
    # your choice if you want to set a threshold (ttl=90 with 30s threshold)
    async def retrieve_services(self, library_id: int | None = None) -> dict:
        """Retrieves available services, optionally filtered by library.

        Parameters
        ----------
        library_id : int | None, optional
            Library ID to filter services by, by default None.

        Returns
        -------
        dict
            Mapping of service IDs to their type, duration options, and
            resource counts.
        """
        if library_id:
            filtro = {"area": {"id": library_id}}
            cookie_value = quote(json.dumps(filtro, separators=(",", ":")))
            cookie = {"entry-filters": cookie_value}
        else:
            cookie = None

        endpoint = f"/servizi/{self.client_id}"
        response = await self.client.get(endpoint, cookies=cookie)
        all_seats = response.json()["servizi"]
        params = ["type", "durate_select", "numero_risorse"]
        service_dict = {}
        for lib in all_seats:
            service_dict |= {lib["id"]: {i: lib[i] for i in params}}

        return service_dict

    async def retrieve_free_seats(
        self,
        library_id: int,
        service_id: int,
        time_amount: int = 3600,
        days_forward: int = 0,
    ) -> dict:
        """Retrieves available seat slots for a specific library and service.

        Parameters
        ----------
        library_id : int
            ID of the library.
        service_id : int
            ID of the service within the library.
        time_amount : int, optional
            Booking duration in seconds, by default 3600.
        days_forward : int, optional
            Number of days ahead of today (Europe/Rome timezone), by default 0.

        Returns
        -------
        dict
            JSON schedule response with available time slots.
        """
        await self._refresh_cookie()

        tz_roma = zoneinfo.ZoneInfo("Europe/Rome")
        date = datetime.datetime.now(tz_roma).date() + datetime.timedelta(
            days=days_forward
        )

        endpoint = f"/entry/{service_id}/schedule/{date}/{library_id}/{time_amount}?user_primary={self.user.tax_id}"

        result = await self.client.get(endpoint)
        return result.json()

    async def retrieve_all_free_seats(
        self, library_id: int, time_amount: int = 3600, days_forward: int = 0
    ) -> dict:
        # will call retrieve_free_seats for all services concurrently->TODO cuncurrency
        services = await self.retrieve_services(library_id)
        for s in services:
            seats = await self.retrieve_free_seats(
                library_id, s, time_amount, days_forward
            )
            services[s]["seats"] = seats
        return services

    # BOOKINGS #

    async def retrieve_filters(self) -> dict:
        "Returns all services and area ids"

        endpoint = "/reservations/filter"

        headers = {
            "x-app-locale": "it",
            "x-cliente": self.client_id,
        }

        response = await self.client.get(
            endpoint,
            headers=headers,
        )

        return response.json()

    async def booking_info(
        self,
        page_number: int = 1,
        sortby: str = "data",
        order: str = "desc",
        filters: dict = None,
    ) -> dict:

        if filters is None:
            filters = {}
        payload = {"filters": filters, "user": self.token}
        params = {"page": page_number, "sortBy": sortby, "order": order}
        endpoint = f"/reservations/{self.client_id}"

        response = await self.client.post(endpoint, params=params, json=payload)
        return response.json()

    async def all_booking_info(
        self,
        sortby: str = "data",
        order: str = "desc",
        filters: dict = None,
    ) -> dict:  # will only return the ["data"]
        """Retrieves all pages of the user's bookings combined.

        Parameters
        ----------
        sortby : str, optional
            Field to sort bookings by, by default "data".
        order : str, optional
            Sort direction (``"asc"`` or ``"desc"``), by default "desc".
        filters : dict, optional
            Filter criteria to apply to the query, by default None.

        Returns
        -------
        dict
            Dictionary with a single ``"data"`` key containing all bookings.
        """
        data_dict = {}
        # first page
        page_1 = await self.booking_info(sortby=sortby, order=order, filters=filters)
        total_pages = int(page_1["meta"]["last_page"])

        data_dict["data"] = page_1["data"]
        if total_pages > 1:
            for i in range(2, total_pages + 1):
                page = await self.booking_info(
                    page_number=i, sortby=sortby, order=order, filters=filters
                )
                data_dict["data"] += page["data"]

        return data_dict

    async def last_booking(self) -> dict:
        """Retrieves the user's most recent booking.

        Returns
        -------
        dict
            JSON response containing details of the latest booking.
        """
        endpoint = f"/reservations/last/{self.client_id}"
        payload = {"user": self.token}

        response = await self.client.post(endpoint, json=payload)
        return response.json()

    async def booking_exceptions(
        self, service_id: int
    ) -> dict:  # returns just last exception no matter the id?
        endpoint = f"/reservations/exceptions/{self.client_id}/{service_id}"
        payload = {"user": self.token}

        response = await self.client.post(endpoint, json=payload)
        return response.json()

    async def manage_single_booking(self, booking_code: str):
        """Retrieves management details for a single booking.

        Parameters
        ----------
        booking_code : str
            Alphanumerical booking code.

        Returns
        -------
        dict
            JSON response containing booking management data.
        """
        params = {"chiave": self.user.tax_id}
        endpoint = f"entry/manage/{booking_code}"

        response = await self.client.get(endpoint, params=params)
        return response.json()

    async def single_booking_info(self, booking_id: str) -> int:
        """Returns the id of the service which the booking id refers to

        Parameters
        ----------
        booking_id : str
            booking alphanumerical code

        Returns
        -------
        int
            service id number
        """
        endpoint = f"/entry/{booking_id}/info"
        params = {"chiave": self.user.tax_id}

        response = await self.client.get(endpoint, params=params)
        return response.json()

    async def _update_booking(self, booking_id: str, update_to: dict):
        """Sends an update request for an existing booking.

        Parameters
        ----------
        booking_id : str
            Alphanumerical booking code.
        update_to : dict
            Payload describing the update to apply.

        Returns
        -------
        dict
            JSON response returned by the update endpoint.
        """
        await self._refresh_cookie()
        await asyncio.sleep(10)  # otherwise it would call raise an error

        endpoint = f"entry/update/{booking_id}"
        params = {"chiave": self.user.tax_id}

        base = "https://prenotabiblio.sba.unimi.it"
        referer_url = str(
            httpx2.URL(
                f"{base}/portalePlanning/biblio/gestisci/show/{booking_id}",
                params={"primaria": self.user.tax_id},
            )
        )
        headers = {"referer": referer_url}

        response = await self.client.post(
            endpoint, json=update_to, params=params, headers=headers
        )

        return response.json()

    async def free_up_seat(self, booking_id: str):
        """Releases the seat associated with an active booking.

        Parameters
        ----------
        booking_id : str
            Alphanumerical booking code.

        Returns
        -------
        dict
            JSON response returned by the update endpoint.
        """
        return await self._update_booking(
            booking_id=booking_id, update_to={"type": "libera_posto"}
        )

    async def delete_booking(self, booking_id: str):
        """Cancels and deletes an existing booking.

        Parameters
        ----------
        booking_id : str
            Alphanumerical booking code.

        Returns
        -------
        dict
            JSON response returned by the delete endpoint.
        """
        await self._refresh_cookie()

        endpoint = f"entry/delete/{booking_id}"
        params = {"chiave": self.user.tax_id}

        base = "https://prenotabiblio.sba.unimi.it"
        referer_url = str(
            httpx2.URL(
                f"{base}/portalePlanning/biblio/gestisci/show/{booking_id}",
                params={"primaria": self.user.tax_id},
            )
        )
        headers = {"referer": referer_url}

        result = await self.client.post(endpoint, params=params, headers=headers)
        return result.json()

    async def update_booking_info(
        self, booking_id: str, new_tax_id: str, new_fullname: str, new_email: str
    ):
        """Updates the attendee information on an existing booking.

        Parameters
        ----------
        booking_id : str
            Alphanumerical booking code.
        new_tax_id : str
            New tax ID (codice fiscale) for the booking.
        new_fullname : str
            New full name (surname and name) for the booking.
        new_email : str
            New email address for the booking.

        Returns
        -------
        dict
            JSON response returned by the update endpoint.
        """
        new_info = {
            "codice_fiscale": new_tax_id,
            "cognome_nome": new_fullname,
            "email": new_email,
        }
        result = await self._update_booking(booking_id, update_to=new_info)
        return result

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
    def __init__(self, unimi_user: UnimiUser | None = None, proxy_url: HttpUrl = None):

        self.user = unimi_user

        # generate headers
        hgen = HeaderGenerator()
        headers = hgen.generate()
        # setup client
        self.client = httpx2.AsyncClient(
            follow_redirects=True,
            headers=headers,
            base_url="https://prenotabiblio.sba.unimi.it/portalePlanningAPI/api",
            proxy=proxy_url,
        )

        self.token = ""  # saved in the client memory
        self.client_id = 2  # per EasyStaff unimi è il 2 cliente

    async def close_connection(self):
        await self.client.aclose()

    def _build_login_payload(self, login_page: httpx2.Response):
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

        payload["username"] = (self.user.email,)
        payload["selTipoUtente"] = (self.user.user_type,)
        payload["password"] = self.user.password.get_secret_value()

        return payload

    def _extract_token_endpoint(self, login_landing: httpx2.Response):
        soup = BeautifulSoup(login_landing.text, "html.parser")
        script = soup.find("script", type="text/javascript").get_text()

        token_endpoint = script.split('"')[
            1
        ]  # the token is a string in double quotes (" ")

        # save the user token
        self.token = token_endpoint.split("/auth/me/")[1]

        return token_endpoint

    async def validate_token(self) -> bool:
        # need to set a try except block
        # makes sense to execute before time expencive operations
        # to check if still logged in
        val_url = "/auth/validateJWT"
        val_check = await self.client.get(val_url)

        return val_check.json()["valid"]

    async def login(self) -> None:
        """Logins the user to the unimi website

        Parameters
        ----------
        user : UnimiUser
            User registered on the unimi website
        """
        if not self.user:
            raise MissingUserError("No user passed")

        login_url = "https://cas.unimi.it/login?service=https%3A%2F%2Fprenotabiblio.sba.unimi.it%2Fauth%2Fauth_planning.php"

        login_page = await self.client.get(login_url)

        payload = self._build_login_payload(login_page=login_page)

        login_landing = await self.client.post(login_url, data=payload)

        token_endpoint = self._extract_token_endpoint(
            login_landing=login_landing
        )  # the url that lets you login to the library user page
        # we populate the UnimiUser data
        self.user.get_data_from_jsw(self.token)
        await self.client.get(
            token_endpoint
        )  # server will release a cookie that will be stored in the client

    @alru_cache(ttl=90)  # cookie would reset after 120s but we use 30s threshold
    async def retrive_available_libraries(self) -> dict:
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
        await self.retrive_available_libraries()  # refresh the cookie

        tz_roma = zoneinfo.ZoneInfo("Europe/Rome")
        date = datetime.datetime.now(tz_roma).date() + datetime.timedelta(
            days=days_forward
        )

        endpoint = f"""/entry/{service_id}/schedule/{date}
                    /{library_id}/{time_amount}?user_primary={self.user.tax_id}"""

        result = await self.client.get(endpoint)
        return result.json()

    async def retrieve_all_free_seats(
        self, library_id: int, time_amount: int = 3600, days_forward: int = 0
    ) -> dict:
        # will call retrieve_free_seats for all services concurrently
        services = await self.retrieve_services(library_id)
        for s in services:
            seats = await self.retrieve_free_seats(
                library_id, s, time_amount, days_forward
            )
            services[s]["seats"] = seats
        return services

    # BOOKINGS#

    async def retrieve_filters(self) -> dict:
        "Returns all services and area ids"

        endpoint = "/reservations/filter"

        headers = {
            "x-app-locale": "it",
            "x-cliente": "2",
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
    ) -> dict: # will only return the ["data"]

        data_dict={}
        #first page
        page_1=await self.booking_info(sortby=sortby,order=order,filters=filters)
        total_pages=int(page_1["meta"]["last_page"])

        data_dict["data"]=page_1["data"]
        if total_pages > 1:
            for i in range(2,total_pages+1):
                page=await self.booking_info(page_number=i,sortby=sortby,order=order,filters=filters)
                data_dict["data"]+=page["data"]

        return data_dict


    async def last_booking(self) -> dict:
        endpoint = f"/reservations/last/{self.client_id}"
        payload = {"user": self.token}

        response = await self.client.post(endpoint, json=payload)
        return response.json()

    async def booking_exeptions(self,service_id:int) -> dict: # returns just last exeption no matter the id? 
        endpoint=f"/reservations/exceptions/{self.client_id}/{service_id}"
        payload = {"user": self.token}

        response = await self.client.post(endpoint, json=payload)
        return response.json()
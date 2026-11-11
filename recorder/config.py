"""Static configuration: the five cities, their Kalshi series and NWS stations.

Station coordinates and grid points were checked on 2026-11-10 against
api.weather.gov (/stations/{id} and /points/{lat},{lon}). The recorder
re-fetches /points on every capture, so a grid change shows up in the data
instead of silently going stale.
"""
from dataclasses import dataclass

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
NWS_BASE = "https://api.weather.gov"
CLI_URL = (
    "https://forecast.weather.gov/product.php"
    "?site={site}&issuedby={cli_id}&product=CLI&format=TXT&version=1&glossary=0"
)

# Local hour (IANA civil time) at which each city's forecast is frozen.
CUTOFF_HOUR = 18


@dataclass(frozen=True)
class City:
    key: str             # short folder name
    series: str          # Kalshi series ticker
    station: str         # NWS observing station named by the contract
    cli_site: str        # NWS office that issues the climate (CLI) report
    cli_id: str          # CLI location id (CLI + this = e.g. CLINYC)
    lat: float
    lon: float
    grid_office: str
    grid_x: int
    grid_y: int
    tz: str              # IANA zone used for the 6 p.m. cutoff
    lst_utc_offset: int  # local STANDARD time offset; defines the climate day

    @property
    def points_url(self) -> str:
        return f"{NWS_BASE}/points/{self.lat:.4f},{self.lon:.4f}"

    @property
    def grid_url(self) -> str:
        return f"{NWS_BASE}/gridpoints/{self.grid_office}/{self.grid_x},{self.grid_y}"

    @property
    def cli_url(self) -> str:
        return CLI_URL.format(site=self.cli_site, cli_id=self.cli_id)


CITIES = [
    City("NY", "KXHIGHNY", "KNYC", "OKX", "NYC", 40.7833, -73.9667, "OKX", 34, 45, "America/New_York", -5),
    City("CHI", "KXHIGHCHI", "KMDW", "LOT", "MDW", 41.7842, -87.7553, "LOT", 72, 69, "America/Chicago", -6),
    City("MIA", "KXHIGHMIA", "KMIA", "MFL", "MIA", 25.7906, -80.3164, "MFL", 105, 51, "America/New_York", -5),
    City("DEN", "KXHIGHDEN", "KDEN", "BOU", "DEN", 39.8466, -104.6562, "BOU", 75, 66, "America/Denver", -7),
    City("LAX", "KXHIGHLAX", "KLAX", "LOX", "LAX", 33.9381, -118.3889, "LOX", 149, 41, "America/Los_Angeles", -8),
]

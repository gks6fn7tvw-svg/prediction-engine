# Verified station/grid mappings

| City | Series / CLI | NWS station | NWS coordinates (lat, lon) | Grid | Hourly forecast |
|---|---|---|---|---|---|
| Chicago | KXHIGHCHI / CLIMDW | KMDW — Chicago, Chicago Midway Airport | 41.78417, -87.75528 | LOT/72,69 | https://api.weather.gov/gridpoints/LOT/72,69/forecast/hourly |
| Denver | KXHIGHDEN / CLIDEN | KDEN — Denver, Denver International Airport | 39.84658, -104.65622 | BOU/75,66 | https://api.weather.gov/gridpoints/BOU/75,66/forecast/hourly |
| Los Angeles | KXHIGHLAX / CLILAX | KLAX — Los Angeles, Los Angeles International Airport | 33.93806, -118.38889 | LOX/149,41 | https://api.weather.gov/gridpoints/LOX/149,41/forecast/hourly |
| Miami | KXHIGHMIA / CLIMIA | KMIA — Miami, Miami International Airport | 25.79056, -80.31639 | MFL/105,51 | https://api.weather.gov/gridpoints/MFL/105,51/forecast/hourly |
| NYC | KXHIGHNY / CLINYC | KNYC — New York City, Central Park | 40.78333, -73.96667 | OKX/34,45 | https://api.weather.gov/gridpoints/OKX/34,45/forecast/hourly |

Derived from current official station geometries and /points responses; refresh mappings periodically. Full raw responses, current CLI station-name evidence and retrieval metadata accompany this table.

Official station endpoint: https://api.weather.gov/stations/{station}. Point lookup: https://api.weather.gov/points/{latitude},{longitude}, using four decimal places as captured.

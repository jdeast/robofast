"""Site adapters: parsers for each site's weather pages.

Importing this package registers every adapter's source types in
:data:`robofast_core.weather_sources.SOURCE_TYPES`.
"""

from robofast_core.sites import minerva

minerva.register()

"""
Open WebUI Inlet Filter: Offline Map Search via Photon
Detects location-related queries and searches the local Photon
geocoding service for relevant places, addresses, and points of interest.
Injects results with coordinates and source attribution.
"""

import urllib.parse
import re
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=6, description="Filter priority"
        )
        photon_url: str = Field(
            default="http://krull-photon:2322",
            description="Photon geocoding instance URL",
        )
        tileserver_public_url: str = Field(
            default="http://localhost:8070",
            description="TileServer GL public URL for map links",
        )
        num_results: int = Field(
            default=5, description="Number of search results to include"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable map search"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _is_location_query(self, text: str) -> bool:
        """Detect if the query is location-related."""
        location_patterns = [
            r"\b(?:where|find|locate|nearest|nearby|close to|around)\b",
            r"\b(?:directions?|route|navigate|how (?:do I |to )get to)\b",
            r"\b(?:map|maps|address|location|coordinates?|gps)\b",
            r"\b(?:restaurant|cafe|coffee|shop|store|hotel|hospital|school|park|museum|library|airport|station)\b",
            r"\b(?:street|road|avenue|boulevard|highway|drive|lane|plaza)\b",
            r"\b(?:city|town|county|state|country|region|district|neighborhood)\b",
            r"\b(?:latitude|longitude|lat|lon|lng)\b",
            r"\b(?:zip\s*code|postal\s*code)\b",
        ]
        text_lower = text.lower()
        matches = sum(
            1 for p in location_patterns if re.search(p, text_lower)
        )
        return matches >= 1

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return body

        query = last_message.get("content", "")
        if not query or len(query.strip()) < 3:
            return body

        if not self._is_location_query(query):
            return body

        try:
            import aiohttp

            search_url = (
                f"{self.valves.photon_url}/api"
                f"?q={urllib.parse.quote(query)}"
                f"&limit={self.valves.num_results}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return body
                    data = await resp.json()

            features = data.get("features", [])
            if not features:
                return body

            context_lines = ["[Offline Map Search Results — OpenStreetMap via Photon]"]
            for i, feature in enumerate(features, 1):
                props = feature.get("properties", {})
                geom = feature.get("geometry", {})
                coords = geom.get("coordinates", [])

                name = props.get("name", "")
                street = props.get("street", "")
                housenumber = props.get("housenumber", "")
                city = props.get("city", "")
                state = props.get("state", "")
                country = props.get("country", "")
                osm_type = props.get("osm_value", props.get("type", ""))

                # Build address
                parts = []
                if housenumber and street:
                    parts.append(f"{housenumber} {street}")
                elif street:
                    parts.append(street)
                if city:
                    parts.append(city)
                if state:
                    parts.append(state)
                if country:
                    parts.append(country)
                address = ", ".join(parts)

                line = f"{i}. {name}" if name else f"{i}. {address}"
                if name and address:
                    line += f"\n   Address: {address}"
                if osm_type:
                    line += f"\n   Type: {osm_type}"
                if len(coords) >= 2:
                    lon, lat = coords[0], coords[1]
                    line += f"\n   Coordinates: {lat:.6f}, {lon:.6f}"
                    line += f"\n   Map: {self.valves.tileserver_public_url}/#17/{lat}/{lon}"

                context_lines.append(line)

            context_lines.append("[End Map Search Results]")
            context_lines.append("")
            context_lines.append(
                "IMPORTANT: When using location information from the map search "
                "results above, cite OpenStreetMap as the source. Include "
                "coordinates and addresses in your response. Add a References "
                "section at the end crediting OpenStreetMap (openstreetmap.org)."
            )
            context_lines.append("")

            map_context = "\n".join(context_lines)
            messages[-1]["content"] = (
                f"{map_context}\nUser question: {query}"
            )

        except Exception:
            pass

        return body

"""
Global city data for adaptive location question.
Maps ISO country codes to top cities, used by question_engine.py.
"""

# ISO 3166-1 alpha-2 → top cities (ordered by relevance for job seekers)
COUNTRY_TOP_CITIES: dict[str, list[str]] = {
    "IN": ["Bengaluru", "Mumbai", "Delhi NCR", "Hyderabad", "Pune", "Chennai", "Kolkata", "Ahmedabad"],
    "US": ["New York", "San Francisco", "Seattle", "Austin", "Boston", "Chicago", "Los Angeles", "Atlanta"],
    "GB": ["London", "Manchester", "Edinburgh", "Bristol", "Birmingham", "Leeds", "Cambridge", "Oxford"],
    "SG": ["Singapore"],
    "AE": ["Dubai", "Abu Dhabi"],
    "AU": ["Sydney", "Melbourne", "Brisbane", "Perth"],
    "CA": ["Toronto", "Vancouver", "Montreal", "Calgary"],
    "DE": ["Berlin", "Munich", "Hamburg", "Frankfurt"],
    "NL": ["Amsterdam", "Rotterdam", "The Hague"],
    "FR": ["Paris", "Lyon", "Bordeaux"],
    "JP": ["Tokyo", "Osaka"],
    "HK": ["Hong Kong"],
    "NZ": ["Auckland", "Wellington"],
    "IE": ["Dublin"],
    "CH": ["Zurich", "Geneva"],
    "SE": ["Stockholm", "Gothenburg"],
    "DK": ["Copenhagen"],
    "NO": ["Oslo"],
    "FI": ["Helsinki"],
    "BE": ["Brussels", "Antwerp"],
    "PK": ["Karachi", "Lahore", "Islamabad"],
    "BD": ["Dhaka"],
    "LK": ["Colombo"],
    "MY": ["Kuala Lumpur", "Penang"],
    "PH": ["Manila", "Cebu"],
    "ID": ["Jakarta", "Bali"],
    "TH": ["Bangkok"],
    "VN": ["Ho Chi Minh City", "Hanoi"],
    "KE": ["Nairobi"],
    "NG": ["Lagos", "Abuja"],
    "ZA": ["Johannesburg", "Cape Town"],
    "EG": ["Cairo"],
    "IL": ["Tel Aviv", "Jerusalem"],
    "SA": ["Riyadh", "Jeddah"],
    "QA": ["Doha"],
    "KW": ["Kuwait City"],
    "BH": ["Manama"],
    "BR": ["Sao Paulo", "Rio de Janeiro"],
    "MX": ["Mexico City", "Guadalajara"],
    "AR": ["Buenos Aires"],
    "CL": ["Santiago"],
    "CO": ["Bogota", "Medellin"],
}

# Text aliases for country name → ISO code (used when parsing resume text)
COUNTRY_NAME_TO_CODE: dict[str, str] = {
    "india": "IN", "indian": "IN",
    "united states": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "u.k.": "GB", "england": "GB", "britain": "GB",
    "singapore": "SG",
    "uae": "AE", "united arab emirates": "AE", "dubai": "AE", "abu dhabi": "AE",
    "australia": "AU", "australian": "AU",
    "canada": "CA", "canadian": "CA",
    "germany": "DE", "german": "DE",
    "netherlands": "NL", "holland": "NL",
    "france": "FR", "french": "FR",
    "japan": "JP", "japanese": "JP",
    "hong kong": "HK",
    "new zealand": "NZ",
    "ireland": "IE",
    "switzerland": "CH",
    "sweden": "SE",
    "denmark": "DK",
    "norway": "NO",
    "finland": "FI",
    "belgium": "BE",
    "pakistan": "PK",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "malaysia": "MY",
    "philippines": "PH",
    "indonesia": "ID",
    "thailand": "TH",
    "vietnam": "VN",
    "kenya": "KE",
    "nigeria": "NG",
    "south africa": "ZA",
    "egypt": "EG",
    "israel": "IL",
    "saudi arabia": "SA", "ksa": "SA",
    "qatar": "QA",
    "kuwait": "KW",
    "bahrain": "BH",
    "brazil": "BR",
    "mexico": "MX",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
}

# City name → ISO country code (for direct city detection in resume text)
CITY_TO_COUNTRY: dict[str, str] = {
    # India
    "bengaluru": "IN", "bangalore": "IN", "mumbai": "IN", "bombay": "IN",
    "delhi": "IN", "new delhi": "IN", "hyderabad": "IN", "pune": "IN",
    "chennai": "IN", "madras": "IN", "kolkata": "IN", "calcutta": "IN",
    "ahmedabad": "IN", "kochi": "IN", "cochin": "IN", "jaipur": "IN",
    "chandigarh": "IN", "noida": "IN", "gurugram": "IN", "gurgaon": "IN",
    # US
    "new york": "US", "nyc": "US", "san francisco": "US", "sf": "US",
    "seattle": "US", "austin": "US", "boston": "US", "chicago": "US",
    "los angeles": "US", "la": "US", "atlanta": "US", "bay area": "US",
    "silicon valley": "US", "new jersey": "US",
    # UK
    "london": "GB", "manchester": "GB", "edinburgh": "GB",
    "bristol": "GB", "birmingham": "GB", "leeds": "GB",
    # SG/AE
    "singapore": "SG",
    "dubai": "AE", "abu dhabi": "AE",
    # Other
    "toronto": "CA", "vancouver": "CA", "montreal": "CA",
    "sydney": "AU", "melbourne": "AU",
    "berlin": "DE", "munich": "DE",
    "amsterdam": "NL",
    "paris": "FR",
    "tokyo": "JP",
    "hong kong": "HK",
    "kuala lumpur": "MY",
    "bangkok": "TH",
    "jakarta": "ID",
    "nairobi": "KE",
    "lagos": "NG",
    "cape town": "ZA", "johannesburg": "ZA",
    "tel aviv": "IL",
    "riyadh": "SA",
    "doha": "QA",
    "sao paulo": "BR",
    "mexico city": "MX",
}


def get_cities_for_country(country_code: str, limit: int = 6) -> list[str]:
    """Return top cities for a country code, up to `limit`."""
    return COUNTRY_TOP_CITIES.get(country_code.upper(), [])[:limit]


def detect_country_from_text(text: str) -> str | None:
    """
    Detect country code from resume text by scanning for city/country names.
    Returns ISO 3166-1 alpha-2 code or None.
    Checks first 1500 characters for speed.
    """
    lower = text[:1500].lower()

    # City match first (more specific)
    for city, code in CITY_TO_COUNTRY.items():
        if city in lower:
            return code

    # Country name fallback
    for name, code in COUNTRY_NAME_TO_CODE.items():
        if name in lower:
            return code

    return None

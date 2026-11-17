"""All tunable rules in one place."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DB = os.path.join(DATA, "db")
RULES_VERSION = 3
USER_AGENT = "spring-radar/3 (github.com/gummihurdal/Jackpot)"
# The SEC asks every automated reader to name a contact e-mail address. Put a real one of yours here,
# or set the RADAR_CONTACT environment variable.
CONTACT = os.environ.get("RADAR_CONTACT", "contact@azurenexus.com")

# universe
MIN_CAP_USD = 3e8
MAX_CAP_USD = 1e10
MIN_DVOL_USD = 1e6

# price pattern
MIN_RATIO, MAX_POS = 2.0, 0.25          # main track
PH_MIN_RATIO, PH_MAX_POS = 1.6, 0.40    # pharma track
MAX_DEEP, MAX_DEEP_PHARMA = 1200, 450

# safety gate
MIN_RUNWAY_YEARS = 2.0
MAX_SHARE_GROWTH_3Y = 0.20
MAX_NET_DEBT_EBITDA = 3.5
MAX_NET_DEBT_EBITDA_ASSET_HEAVY = 6.0   # utilities, real estate
MIN_CURRENT_RATIO = 0.8

# caches (days)
TTL_INFO, TTL_SHARES, TTL_TRIALS = 5, 14, 7
INFO_PRICE_MOVE = 0.15                  # refetch fundamentals early if price moved this much

TRACK_TOP, TRACK_TOP_PHARMA = 40, 15
NEWS_DAYS = 120

# country -> (primary exchanges, main currency)
REGIONS = {
    "us": (["NMS", "NYQ", "NGM", "NCM", "ASE"], "USD"), "ca": (["TOR"], "CAD"),
    "gb": (["LSE"], "GBP"), "de": (["GER"], "EUR"), "fr": (["PAR"], "EUR"), "ch": (["EBS"], "CHF"),
    "nl": (["AMS"], "EUR"), "be": (["BRU"], "EUR"), "se": (["STO"], "SEK"), "no": (["OSL"], "NOK"),
    "dk": (["CPH"], "DKK"), "fi": (["HEL"], "EUR"), "it": (["MIL"], "EUR"), "es": (["MCE"], "EUR"),
    "pt": (["LIS"], "EUR"), "at": (["VIE"], "EUR"), "ie": (["ISE"], "EUR"), "pl": (["WSE"], "PLN"),
    "gr": (["ATH"], "EUR"), "il": (["TLV"], "ILS"), "jp": (["JPX"], "JPY"), "hk": (["HKG"], "HKD"),
    "tw": (["TAI"], "TWD"), "kr": (["KSC", "KOE"], "KRW"), "in": (["NSI"], "INR"),
    "sg": (["SES"], "SGD"), "au": (["ASX"], "AUD"), "nz": (["NZE"], "NZD"), "id": (["JKT"], "IDR"),
    "th": (["SET"], "THB"), "my": (["KLS"], "MYR"), "sa": (["SAU"], "SAR"), "za": (["JNB"], "ZAR"),
    "br": (["SAO"], "BRL"), "mx": (["MEX"], "MXN"), "cl": (["SGO"], "CLP"),
}
REGION_NAME = {
    "us": "USA", "ca": "Canada", "gb": "UK", "de": "Germany", "fr": "France", "ch": "Switzerland",
    "nl": "Netherlands", "be": "Belgium", "se": "Sweden", "no": "Norway", "dk": "Denmark",
    "fi": "Finland", "it": "Italy", "es": "Spain", "pt": "Portugal", "at": "Austria", "ie": "Ireland",
    "pl": "Poland", "gr": "Greece", "il": "Israel", "jp": "Japan", "hk": "Hong Kong",
    "tw": "Taiwan", "kr": "South Korea", "in": "India", "sg": "Singapore", "au": "Australia",
    "nz": "New Zealand", "id": "Indonesia", "th": "Thailand", "my": "Malaysia", "sa": "Saudi Arabia",
    "za": "South Africa", "br": "Brazil", "mx": "Mexico", "cl": "Chile",
}
MINOR = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

MINING = {"Gold", "Silver", "Copper", "Other Industrial Metals & Mining", "Other Precious Metals & Mining",
          "Uranium", "Coking Coal", "Thermal Coal", "Aluminum", "Steel"}
PHARMA = {"Drug Manufacturers - General", "Drug Manufacturers - Specialty & Generic", "Biotechnology"}
PHARMA_SCREEN = ["Biotechnology", "Drug Manufacturers—General", "Drug Manufacturers—Specialty & Generic"]
AI_TECH = {"Semiconductors", "Semiconductor Equipment & Materials", "Software - Infrastructure",
           "Software - Application", "Computer Hardware", "Communication Equipment",
           "Electronic Components", "Information Technology Services"}
PHARMA_THEME = "Pharma & Biotech"

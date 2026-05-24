from contextlib import asynccontextmanager
from collections import OrderedDict
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import json
from pydantic import BaseModel
import re
import html
import os
import httpx
import nltk
import emoji as emoji_lib
from nltk.corpus import stopwords, wordnet
from dotenv import load_dotenv
import yt_dlp
from deep_translator import GoogleTranslator
from concrete_words import CONCRETE_WORDS

load_dotenv()

VERSION = "0.2.4"

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")

# Build proxy URL if credentials are set (used by yt-dlp on cloud deployments)
_proxy_user = os.getenv("PROXY_USERNAME", "")
_proxy_pass = os.getenv("PROXY_PASSWORD", "")
PROXY_URL = (
    f"http://{_proxy_user.replace('@', '%40')}:{_proxy_pass.replace('$', '%24')}@p.webshare.io:80"
    if _proxy_user and _proxy_pass else None
)

# Cache processed results for the last 10 videos (resets on server restart)
_cache: OrderedDict = OrderedDict()
CACHE_SIZE = 10

EXTRA_SKIP = {
    "música",  # appears in subtitles as a music-playing indicator, not spoken
    "ya", "muy", "mas", "bien", "mal", "hay", "pues",
    "como", "cuando", "donde", "porque", "aunque", "tambien", "algo",
    "alguien", "nada", "nadie", "cada", "otro", "otra", "creo", "pasa",
    "parte", "tipo", "vez", "veces", "anos", "tiempo", "mundo", "vida",
    "gente", "cosa", "cosas", "hace", "dice", "quiere", "sabe", "viene",
    "pero", "esto", "esta", "este", "esos", "esas", "todo", "toda",
    "ellos", "ellas", "nosotros", "vosotros", "ustedes", "usted",
    "este", "estos", "estas", "esos", "esas", "aquel", "aquella",
    "tengo", "tiene", "tienes", "tenemos", "tienen", "tenia",
    "estoy", "estas", "estamos", "estan", "estaba", "estuvo",
    "puedo", "puede", "puedes", "podemos", "pueden", "podia",
    "quiero", "quiere", "quieres", "queremos", "quieren",
    "voy", "vamos", "vayas", "fueron", "seria", "habia",
}

# Built at startup: english_word -> emoji_char
emoji_lookup: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    nltk.download("stopwords", quiet=True)
    nltk.download("wordnet", quiet=True)
    for char, data in emoji_lib.EMOJI_DATA.items():
        name = data.get("en", "").strip(":").lower().replace("_", " ").strip()
        if name:
            emoji_lookup.setdefault(name, char)
        for alias in data.get("alias", []):
            clean = alias.strip(":").replace("_", " ").lower().strip()
            if clean:
                emoji_lookup.setdefault(clean, char)
    yield


app = FastAPI(lifespan=lifespan)


class VideoRequest(BaseModel):
    url: str


def extract_video_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|embed/)([a-zA-Z0-9_-]{11})", url)
    if not match:
        raise ValueError("Invalid YouTube URL")
    return match.group(1)


def clean_text(text: str) -> str:
    text = html.unescape(text)
    return re.sub(r"<[^>]+>", "", text).strip()


def get_candidates(transcript):
    """One non-stopword candidate per segment, no time-gap filtering yet."""
    try:
        stops = set(stopwords.words("spanish"))
    except Exception:
        stops = set()
    stops.update(EXTRA_SKIP)

    candidates = []

    for seg in transcript:
        t = seg["start"]
        duration = seg.get("duration", 2.0)
        text = re.sub(r"[^\w\s]", "", clean_text(seg["text"]).lower())
        words_in_seg = text.split()
        total = max(len(words_in_seg), 1)
        seen_in_seg = set()

        for idx, word in enumerate(words_in_seg):
            if (
                len(word) >= 1
                and (word.isalpha() or word.isdigit())
                and (len(word) >= 3 or word.isdigit())
                and word not in stops
                and word not in seen_in_seg
            ):
                estimated = round(t + (idx / total) * duration, 2)
                candidates.append({"time": estimated, "word": word})
                seen_in_seg.add(word)

    return candidates


def assess_level(transcript) -> str:
    all_text = " ".join(clean_text(s["text"]) for s in transcript)
    words = re.findall(r"[a-záéíóúüñ]+", all_text.lower())
    if not words:
        return "Unknown"

    total_time = transcript[-1]["start"] + transcript[-1].get("duration", 0)
    wpm = len(words) / max(total_time / 60, 1)

    # Type-token ratio over a fixed 500-word window — length-independent
    sample = words[:500]
    unique_ratio = len(set(sample)) / len(sample)

    # Long word ratio: words ≥ 7 letters signal advanced vocabulary
    long_ratio = sum(1 for w in words if len(w) >= 7) / len(words)

    score = 0
    score += 3 if wpm > 160 else 2 if wpm > 125 else 1 if wpm > 90 else 0
    score += 3 if unique_ratio > 0.72 else 2 if unique_ratio > 0.60 else 1 if unique_ratio > 0.47 else 0
    score += 2 if long_ratio > 0.28 else 1 if long_ratio > 0.18 else 0

    if score <= 2:
        return "Super Beginner"
    if score <= 4:
        return "Beginner"
    if score <= 6:
        return "Intermediate"
    return "Advanced"


# Common translation mismatches: what Google Translate gives -> emoji name
EMOJI_SYNONYMS = {
    # ── Animals ───────────────────────────────────────────────────────
    "dog": "dog face",
    "cat": "cat face",
    "horse": "horse face",
    "cow": "cow face",
    "pig": "pig face",
    "sheep": "ewe",
    "goat": "goat",
    "chicken": "chicken",
    "rooster": "rooster",
    "duck": "duck",
    "turkey": "turkey",
    "rabbit": "rabbit face",
    "bunny": "rabbit face",
    "mouse": "mouse face",
    "rat": "rat",
    "hamster": "hamster",
    "elephant": "elephant",
    "lion": "lion",
    "tiger": "tiger face",
    "bear": "bear",
    "wolf": "wolf",
    "fox": "fox",
    "deer": "deer",
    "monkey": "monkey face",
    "gorilla": "gorilla",
    "zebra": "zebra",
    "giraffe": "giraffe",
    "hippo": "hippopotamus",
    "hippopotamus": "hippopotamus",
    "rhino": "rhinoceros",
    "rhinoceros": "rhinoceros",
    "crocodile": "crocodile",
    "alligator": "crocodile",
    "snake": "snake",
    "lizard": "lizard",
    "turtle": "turtle",
    "frog": "frog",
    "toad": "frog",
    "shark": "shark",
    "whale": "whale",
    "dolphin": "dolphin",
    "octopus": "octopus",
    "crab": "crab",
    "lobster": "lobster",
    "shrimp": "shrimp",
    "butterfly": "butterfly",
    "bee": "honeybee",
    "honeybee": "honeybee",
    "ant": "ant",
    "spider": "spider",
    "fly": "fly",
    "mosquito": "mosquito",
    "worm": "worm",
    "eagle": "eagle",
    "owl": "owl",
    "parrot": "parrot",
    "penguin": "penguin",
    "flamingo": "flamingo",
    "peacock": "peacock",
    "bird": "bird",
    "chick": "baby chick",
    "fish": "fish",
    "blowfish": "blowfish",
    "tropical fish": "tropical fish",
    "snail": "snail",
    "bug": "bug",
    "caterpillar": "caterpillar",
    "scorpion": "scorpion",
    "bat": "bat",
    "panda": "panda",
    "koala": "koala",
    "kangaroo": "kangaroo",
    "hedgehog": "hedgehog",
    "otter": "otter",
    "skunk": "skunk",
    "raccoon": "raccoon",
    "sloth": "sloth",
    "mammoth": "mammoth",
    "bison": "bison",
    "ox": "ox",
    "camel": "two-hump camel",
    "llama": "llama",
    "swan": "swan",

    # ── Fruit ─────────────────────────────────────────────────────────
    "apple": "red apple",
    "banana": "banana",
    "orange": "tangerine",
    "lemon": "lemon",
    "lime": "lime",
    "grape": "grapes",
    "grapes": "grapes",
    "strawberry": "strawberry",
    "watermelon": "watermelon",
    "mango": "mango",
    "pineapple": "pineapple",
    "peach": "peach",
    "pear": "pear",
    "cherry": "cherries",
    "cherries": "cherries",
    "plum": "grapes",
    "avocado": "avocado",
    "tomato": "tomato",
    "coconut": "coconut",
    "kiwi": "kiwi fruit",
    "melon": "melon",
    "blueberry": "blueberry",
    "blueberries": "blueberry",
    "cranberry": "grapes",
    "fig": "grapes",

    # ── Vegetables ────────────────────────────────────────────────────
    "potato": "potato",
    "onion": "onion",
    "garlic": "garlic",
    "carrot": "carrot",
    "pepper": "bell pepper",
    "bell pepper": "bell pepper",
    "chili": "hot pepper",
    "chilli": "hot pepper",
    "lettuce": "leafy green",
    "cabbage": "leafy green",
    "kale": "leafy green",
    "spinach": "leafy green",
    "broccoli": "broccoli",
    "mushroom": "mushroom",
    "corn": "ear of corn",
    "pea": "pea pod",
    "peas": "pea pod",
    "bean": "beans",
    "beans": "beans",
    "cucumber": "cucumber",
    "eggplant": "eggplant",
    "aubergine": "eggplant",
    "pumpkin": "jack-o-lantern",

    # ── Meat & protein ────────────────────────────────────────────────
    "meat": "cut of meat",
    "steak": "cut of meat",
    "beef": "cut of meat",
    "pork": "meat on bone",
    "lamb": "meat on bone",
    "bacon": "bacon",
    "sausage": "meat on bone",
    "hotdog": "hot dog",
    "hot dog": "hot dog",
    "egg": "egg",
    "eggs": "egg",

    # ── Dairy & staples ───────────────────────────────────────────────
    "milk": "glass of milk",
    "cheese": "cheese wedge",
    "butter": "butter",
    "bread": "bread",
    "rice": "cooked rice",
    "noodles": "spaghetti",
    "pasta": "spaghetti",
    "salt": "salt",

    # ── Prepared food ─────────────────────────────────────────────────
    "pizza": "pizza",
    "burger": "hamburger",
    "hamburger": "hamburger",
    "sandwich": "sandwich",
    "soup": "pot of food",
    "stew": "pot of food",
    "salad": "green salad",
    "taco": "taco",
    "burrito": "burrito",
    "sushi": "sushi",
    "ramen": "steaming bowl",
    "curry": "curry rice",
    "fries": "french fries",
    "popcorn": "popcorn",
    "pancake": "pancakes",
    "waffle": "waffle",
    "croissant": "croissant",
    "baguette": "baguette bread",
    "pretzel": "pretzel",
    "bagel": "bagel",
    "doughnut": "doughnut",
    "donut": "doughnut",

    # ── Sweets & desserts ─────────────────────────────────────────────
    "cake": "birthday cake",
    "cookie": "cookie",
    "cookies": "cookie",
    "chocolate": "chocolate bar",
    "candy": "candy",
    "lollipop": "lollipop",
    "ice cream": "ice cream",
    "icecream": "ice cream",

    # ── Drinks ────────────────────────────────────────────────────────
    "coffee": "hot beverage",
    "tea": "teacup without handle",
    "cup": "teacup without handle",
    "mug": "hot beverage",
    "glass": "clinking glasses",
    "bottle": "bottle with popping cork",
    "juice": "beverage box",
    "water": "droplet",
    "wine": "wine glass",
    "beer": "beer mug",
    "cocktail": "cocktail glass",
    "champagne": "bottle with popping cork",
    "soda": "beverage box",
    "smoothie": "beverage box",
    "lemonade": "beverage box",

    # ── Body parts ────────────────────────────────────────────────────
    "eye": "eye",
    "eyes": "eyes",
    "ear": "ear",
    "nose": "nose",
    "mouth": "mouth",
    "lips": "lips",
    "lip": "lips",
    "tooth": "tooth",
    "teeth": "tooth",
    "tongue": "tongue",
    "hand": "raised hand",
    "hands": "clapping hands",
    "arm": "flexed biceps",
    "leg": "leg",
    "foot": "foot",
    "feet": "foot",
    "finger": "index pointing up",
    "thumb": "thumbs up",
    "bone": "bone",
    "heart": "red heart",
    "brain": "brain",
    "lungs": "lungs",
    "lung": "lungs",
    "muscle": "flexed biceps",
    "nail": "nail polish",

    # ── People & family ───────────────────────────────────────────────
    "man": "man",
    "woman": "woman",
    "boy": "boy",
    "girl": "girl",
    "baby": "baby",
    "child": "child",
    "person": "person",
    "family": "family",
    "mother": "woman",
    "mom": "woman",
    "father": "man",
    "dad": "man",
    "grandmother": "older woman",
    "grandma": "older woman",
    "grandfather": "older man",
    "grandpa": "older man",
    "friend": "people hugging",
    "friends": "people hugging",
    "couple": "couple with heart",
    "twins": "people with bunny ears",

    # ── Professions ───────────────────────────────────────────────────
    "doctor": "health worker",
    "nurse": "health worker",
    "teacher": "teacher",
    "student": "student",
    "police": "police officer",
    "cop": "police officer",
    "firefighter": "firefighter",
    "farmer": "farmer",
    "chef": "cook",
    "cook": "cooking",
    "pilot": "pilot",
    "mechanic": "mechanic",
    "scientist": "scientist",
    "artist": "artist",
    "singer": "microphone",
    "judge": "judge",
    "prince": "prince",
    "princess": "princess",
    "king": "crown",
    "queen": "crown",
    "guard": "guard",
    "detective": "detective",
    "spy": "detective",

    # ── Clothing & accessories ────────────────────────────────────────
    "shirt": "t-shirt",
    "tshirt": "t-shirt",
    "dress": "dress",
    "pants": "jeans",
    "jeans": "jeans",
    "shorts": "shorts",
    "coat": "coat",
    "jacket": "coat",
    "bikini": "bikini",
    "shoes": "running shoe",
    "sneakers": "running shoe",
    "sandals": "sandal",
    "sandal": "sandal",
    "socks": "socks",
    "sock": "socks",
    "hat": "top hat",
    "cap": "baseball cap",
    "baseball cap": "baseball cap",
    "crown": "crown",
    "scarf": "scarf",
    "gloves": "gloves",
    "glove": "gloves",
    "tie": "necktie",
    "necktie": "necktie",
    "bag": "handbag",
    "handbag": "handbag",
    "purse": "purse",
    "backpack": "backpack",
    "glasses": "eyeglasses",
    "eyeglasses": "eyeglasses",
    "sunglasses": "sunglasses",
    "specs": "eyeglasses",
    "ring": "ring",
    "watch": "watch",
    "umbrella": "umbrella",
    "ribbon": "ribbon",
    "thong": "thong sandal",
    "bikini top": "bikini",
    "bra": "bra",
    "ballet": "ballet shoes",

    # ── Transport ─────────────────────────────────────────────────────
    "car": "automobile",
    "automobile": "automobile",
    "bus": "bus",
    "train": "locomotive",
    "plane": "airplane",
    "airplane": "airplane",
    "boat": "sailboat",
    "sailboat": "sailboat",
    "ship": "ship",
    "bicycle": "bicycle",
    "bike": "bicycle",
    "motorbike": "motorcycle",
    "motorcycle": "motorcycle",
    "truck": "truck",
    "taxi": "taxi",
    "cab": "taxi",
    "helicopter": "helicopter",
    "rocket": "rocket",
    "tractor": "tractor",
    "ambulance": "ambulance",
    "fire truck": "fire engine",
    "firetruck": "fire engine",
    "scooter": "kick scooter",
    "skateboard": "skateboard",
    "submarine": "submarine",
    "ferry": "ferry",
    "speedboat": "speedboat",
    "canoe": "canoe",
    "van": "delivery truck",
    "monorail": "monorail",
    "trolley": "trolleybus",
    "subway": "metro",
    "tram": "tram car",

    # ── Buildings & places ────────────────────────────────────────────
    "house": "house",
    "home": "house",
    "building": "building construction",
    "apartment": "building construction",
    "school": "school",
    "hospital": "hospital",
    "church": "church",
    "mosque": "mosque",
    "temple": "shinto shrine",
    "shrine": "shinto shrine",
    "bank": "bank",
    "hotel": "hotel",
    "restaurant": "fork and knife with plate",
    "cafe": "hot beverage",
    "bar": "cocktail glass",
    "shop": "shopping bags",
    "store": "convenience store",
    "supermarket": "shopping cart",
    "library": "books",
    "cinema": "clapper board",
    "theater": "performing arts",
    "stadium": "stadium",
    "gym": "person lifting weights",
    "beach": "beach with umbrella",
    "airport": "airplane",
    "port": "anchor",
    "castle": "european castle",
    "factory": "factory",
    "garden": "seedling",
    "office": "office building",
    "sofa": "couch and lamp",
    "couch": "couch and lamp",

    # ── Home & furniture ──────────────────────────────────────────────
    "bed": "bed",
    "chair": "chair",
    "door": "door",
    "window": "window",
    "lamp": "lamp",
    "mirror": "mirror",
    "toilet": "toilet",
    "shower": "shower",
    "bath": "bathtub",
    "bathtub": "bathtub",
    "sink": "droplet",
    "stove": "cooking",
    "oven": "cooking",
    "fridge": "hot beverage",
    "refrigerator": "hot beverage",
    "kitchen": "cooking",
    "bathroom": "toilet",
    "bedroom": "bed",
    "living room": "couch and lamp",
    "carpet": "roll of paper",
    "curtain": "roll of paper",
    "broom": "broom",
    "basket": "basket",
    "razor": "razor",
    "lotion": "lotion bottle",

    # ── Nature & environment ──────────────────────────────────────────
    "tree": "deciduous tree",
    "palm tree": "palm tree",
    "evergreen": "evergreen tree",
    "pine": "evergreen tree",
    "cactus": "cactus",
    "bamboo": "evergreen tree",
    "flower": "cherry blossom",
    "rose": "rose",
    "tulip": "tulip",
    "sunflower": "sunflower",
    "blossom": "blossom",
    "daisy": "blossom",
    "grass": "herb",
    "herb": "herb",
    "plant": "seedling",
    "seedling": "seedling",
    "leaf": "leaf fluttering in wind",
    "leaves": "fallen leaf",
    "seed": "seedling",
    "forest": "evergreen tree",
    "jungle": "evergreen tree",
    "mountain": "mountain",
    "volcano": "volcano",
    "island": "desert island",
    "desert": "desert island",
    "sun": "sun",
    "moon": "crescent moon",
    "full moon": "full moon",
    "star": "star",
    "stars": "star",
    "cloud": "cloud",
    "rain": "cloud with rain",
    "snow": "snowflake",
    "snowflake": "snowflake",
    "lightning": "lightning",
    "thunder": "thunder cloud and rain",
    "rainbow": "rainbow",
    "wind": "wind face",
    "fire": "fire",
    "rock": "rock",
    "stone": "rock",
    "wave": "water wave",
    "ocean": "water wave",
    "sea": "water wave",
    "river": "water wave",
    "lake": "water wave",
    "waterfall": "water wave",
    "earth": "globe showing europe-africa",
    "globe": "globe with meridians",
    "world": "globe showing europe-africa",
    "comet": "comet",
    "snowman": "snowman without snow",

    # ── Weather & sky ─────────────────────────────────────────────────
    "sunny": "sun",
    "cloudy": "cloud",
    "rainy": "cloud with rain",
    "snowy": "snowflake",
    "windy": "wind face",
    "foggy": "fog",
    "fog": "fog",
    "sunset": "sunset",
    "sunrise": "sunrise",
    "night": "night with stars",
    "tornado": "tornado",
    "hurricane": "tornado",

    # ── Objects & tools ───────────────────────────────────────────────
    "photo": "camera",
    "photos": "camera",
    "picture": "camera",
    "selfie": "selfie",
    "book": "open book",
    "books": "books",
    "notebook": "notebook",
    "pen": "pen",
    "pencil": "pencil",
    "paper": "page with curl",
    "envelope": "envelope",
    "letter": "envelope",
    "email": "e-mail",
    "stamp": "envelope",
    "phone": "telephone receiver",
    "telephone": "telephone receiver",
    "cell phone": "mobile phone",
    "cellphone": "mobile phone",
    "cellular": "mobile phone",
    "mobile": "mobile phone",
    "computer": "laptop",
    "laptop": "laptop",
    "keyboard": "keyboard",
    "screen": "computer",
    "television": "television",
    "tv": "television",
    "radio": "radio",
    "camera": "camera",
    "video camera": "video camera",
    "clock": "clock face twelve oclock",
    "alarm": "alarm clock",
    "key": "key",
    "lock": "locked",
    "spoon": "spoon",
    "fork": "fork and knife",
    "knife": "kitchen knife",
    "pan": "cooking",
    "pot": "pot of food",
    "bowl": "bowl with spoon",
    "plate": "fork and knife with plate",
    "box": "package",
    "package": "package",
    "ball": "soccer ball",
    "toy": "teddy bear",
    "doll": "teddy bear",
    "teddy bear": "teddy bear",
    "gun": "water pistol",
    "sword": "dagger",
    "hammer": "hammer",
    "saw": "hammer and wrench",
    "wrench": "wrench",
    "screwdriver": "screwdriver",
    "nail": "nail polish",
    "screw": "nut and bolt",
    "rope": "knot",
    "ladder": "ladder",
    "brush": "paintbrush",
    "paintbrush": "paintbrush",
    "comb": "comb",
    "toothbrush": "toothbrush",
    "soap": "soap",
    "candle": "candle",
    "flashlight": "flashlight",
    "torch": "flashlight",
    "map": "world map",
    "flag": "chequered flag",
    "coin": "coin",
    "money": "money bag",
    "cash": "dollar banknote",
    "card": "credit card",
    "ticket": "ticket",
    "scissors": "scissors",
    "magnifier": "magnifying glass tilted left",
    "telescope": "telescope",
    "microscope": "microscope",
    "syringe": "syringe",
    "pill": "pill",
    "pills": "pill",
    "bandage": "adhesive bandage",
    "trophy": "trophy",
    "medal": "sports medal",
    "gem": "gem stone",
    "crystal": "gem stone",
    "diamond": "gem stone",
    "balloon": "balloon",
    "kite": "kite",
    "yarn": "thread",
    "thread": "thread",
    "needle": "sewing needle",
    "magnet": "magnet",
    "battery": "battery",
    "lightbulb": "light bulb",
    "bulb": "light bulb",

    # ── Sports & activities ───────────────────────────────────────────
    "soccer": "soccer ball",
    "football": "american football",
    "basketball": "basketball",
    "tennis": "tennis",
    "baseball": "baseball",
    "softball": "softball",
    "volleyball": "volleyball",
    "rugby": "rugby football",
    "golf": "golf",
    "bowling": "bowling",
    "cricket": "cricket game",
    "hockey": "ice hockey",
    "skiing": "skier",
    "snowboarding": "snowboarder",
    "surfing": "person surfing",
    "swimming": "person swimming",
    "running": "person running",
    "cycling": "person biking",
    "biking": "person biking",
    "walking": "person walking",
    "hiking": "person walking",
    "climbing": "person climbing",
    "weightlifting": "person lifting weights",
    "boxing": "boxing glove",
    "wrestling": "person wrestling",
    "yoga": "person in lotus position",
    "dancing": "woman dancing",
    "archery": "bow and arrow",
    "fishing": "fishing pole",
    "hunting": "bow and arrow",
    "gymnastics": "woman cartwheeling",
    "skateboarding": "skateboard",
    "skating": "ice skate",
    "martial arts": "martial arts uniform",
    "karate": "martial arts uniform",
    "fencing": "person fencing",
    "rowing": "person rowing boat",
    "parachute": "parachute",
    "diving": "person swimming",
    "lacrosse": "lacrosse",

    # ── Emotions & expressions ────────────────────────────────────────
    "happy": "grinning face",
    "sad": "crying face",
    "angry": "angry face",
    "scared": "fearful face",
    "surprised": "astonished face",
    "confused": "confused face",
    "excited": "star-struck",
    "bored": "yawning face",
    "tired": "sleeping face",
    "sleeping": "sleeping face",
    "crying": "crying face",
    "laughing": "grinning squinting face",
    "thinking": "thinking face",
    "disgusted": "nauseated face",
    "embarrassed": "flushed face",
    "nervous": "anxious face with sweat",
    "worried": "worried face",
    "shocked": "astonished face",
    "love": "smiling face with heart-eyes",
    "cool": "smiling face with sunglasses",
    "silly": "winking face with tongue",
    "crazy": "face with spiral eyes",
    "sick": "sneezing face",
    "hurt": "crying face",
    "jealous": "face with steam from nose",
    "proud": "smiling face with smiling eyes",

    # ── Abstract but picturable ───────────────────────────────────────
    "look": "eye",
    "see": "eye",
    "hear": "ear",
    "listen": "ear",
    "speak": "speech balloon",
    "talk": "speech balloon",
    "say": "speech balloon",
    "think": "thought balloon",
    "like": "thumbs up",
    "dislike": "thumbs down",
    # Good / bad
    "good": "thumbs up",
    "great": "thumbs up",
    "excellent": "thumbs up",
    "fantastic": "thumbs up",
    "perfect": "thumbs up",
    "wonderful": "thumbs up",
    "amazing": "thumbs up",
    "incredible": "thumbs up",
    "awesome": "thumbs up",
    "superb": "thumbs up",
    "magnificent": "thumbs up",
    "bad": "thumbs down",
    "terrible": "thumbs down",
    "horrible": "thumbs down",
    "awful": "thumbs down",
    "dreadful": "thumbs down",
    "worst": "thumbs down",
    "evil": "smiling face with horns",
    "yes": "check mark button",
    "ok": "ok hand",
    "no": "cross mark",
    "stop": "stop sign",
    "go": "green circle",
    "run": "person running",
    "sleep": "sleeping face",
    "eat": "fork and knife",
    "drink": "beverage box",
    "music": "musical notes",
    "song": "musical note",
    "songs": "musical notes",
    "sound": "speaker high volume",
    "noise": "speaker high volume",
    "silence": "muted speaker",
    "light": "light bulb",
    "dark": "crescent moon",
    "time": "hourglass done",
    "party": "party popper",
    "celebration": "party popper",
    "birthday": "birthday cake",
    "wedding": "wedding",
    "anniversary": "wrapped gift",
    "gift": "wrapped gift",
    "present": "wrapped gift",
    "prize": "trophy",
    "winner": "trophy",
    "war": "crossed swords",
    "battle": "crossed swords",
    "fight": "crossed swords",
    "peace": "peace symbol",
    "death": "skull",
    "skull": "skull",
    "ghost": "ghost",
    "magic": "magic wand",
    "spell": "magic wand",
    "dream": "night with stars",
    "idea": "light bulb",
    "question": "red question mark",
    "answer": "check mark button",
    "problem": "red exclamation mark",
    "danger": "warning",
    "warning": "warning",
    "forbidden": "no entry",
    "love": "red heart",
    "hate": "angry face",

    # ── Numbers ───────────────────────────────────────────────────────
    "0": "keycap 0", "1": "keycap 1", "2": "keycap 2", "3": "keycap 3",
    "4": "keycap 4", "5": "keycap 5", "6": "keycap 6", "7": "keycap 7",
    "8": "keycap 8", "9": "keycap 9", "10": "keycap 10",
    "zero": "keycap 0", "one": "keycap 1", "two": "keycap 2",
    "three": "keycap 3", "four": "keycap 4", "five": "keycap 5",
    "six": "keycap 6", "seven": "keycap 7", "eight": "keycap 8",
    "nine": "keycap 9", "ten": "keycap 10",

    # ── Entertainment & media ─────────────────────────────────────────
    "movie": "clapper board",
    "movies": "clapper board",
    "film": "clapper board",
    "films": "clapper board",
    "concert": "microphone",
    "album": "musical notes",
    "game": "video game",
    "games": "video game",
    "video game": "video game",
    "show": "clapper board",
    "series": "clapper board",
    "episode": "clapper board",
    "entrance": "ticket",

    # ── Work & technology ─────────────────────────────────────────────
    "work": "briefcase",
    "job": "briefcase",
    "meeting": "handshake",
    "internet": "globe with meridians",
    "web": "globe with meridians",
    "website": "globe with meridians",

    # ── Characters & fantasy ──────────────────────────────────────────
    "pirate": "pirate flag",
    "robot": "robot",
    "alien": "alien",
    "monster": "ogre",
    "witch": "witch",
    "zombie": "zombie",
    "ninja": "ninja",
    "hero": "superhero",
    "villain": "supervillain",
    "vampire": "vampire",
    "mermaid": "mermaid",
    "fairy": "fairy",
    "angel": "angel",
    "devil": "smiling face with horns",
    "clown": "clown face",
    "snowman": "snowman without snow",

    # ── Disasters & events ────────────────────────────────────────────
    "earthquake": "volcano",
    "tsunami": "water wave",
    "flood": "water wave",
    "hurricane": "tornado",
    "tornado": "tornado",
    "disaster": "collision",
    "accident": "collision",
    "explosion": "collision",

    # ── Countries & nationalities ─────────────────────────────────────
    # Spain
    "spain": "spain",
    "spanish": "spain",
    "spaniard": "spain",
    # Japan
    "japan": "japan",
    "japanese": "japan",
    # USA
    "united states": "united states",
    "usa": "united states",
    "american": "united states",
    # UK / constituent nations
    "united kingdom": "united kingdom",
    "british": "united kingdom",
    "england": "england",
    "english": "england",
    "scotland": "scotland",
    "scottish": "scotland",
    "wales": "wales",
    "welsh": "wales",
    # France
    "france": "france",
    "french": "france",
    # Germany
    "germany": "germany",
    "german": "germany",
    # Italy
    "italy": "italy",
    "italian": "italy",
    # China
    "china": "china",
    "chinese": "china",
    # Brazil
    "brazil": "brazil",
    "brazilian": "brazil",
    # Argentina
    "argentina": "argentina",
    "argentinian": "argentina",
    "argentine": "argentina",
    # Mexico
    "mexico": "mexico",
    "mexican": "mexico",
    # Portugal
    "portugal": "portugal",
    "portuguese": "portugal",
    # Russia
    "russia": "russia",
    "russian": "russia",
    # South Korea
    "south korea": "south korea",
    "korea": "south korea",
    "korean": "south korea",
    # India
    "india": "india",
    "indian": "india",
    # Australia
    "australia": "australia",
    "australian": "australia",
    # Canada
    "canada": "canada",
    "canadian": "canada",
    # Chile
    "chile": "chile",
    "chilean": "chile",
    # Peru
    "peru": "peru",
    "peruvian": "peru",
    # Cuba
    "cuba": "cuba",
    "cuban": "cuba",
    # Venezuela
    "venezuela": "venezuela",
    "venezuelan": "venezuela",
    # Colombia
    "colombia": "colombia",
    "colombian": "colombia",
    # Costa Rica
    "costa rica": "costa rica",
    "costa rican": "costa rica",
    # Ecuador
    "ecuador": "ecuador",
    "ecuadorian": "ecuador",
    # Paraguay
    "paraguay": "paraguay",
    "paraguayan": "paraguay",
    # Uruguay
    "uruguay": "uruguay",
    "uruguayan": "uruguay",
    # Bolivia
    "bolivia": "bolivia",
    "bolivian": "bolivia",
    # Honduras
    "honduras": "honduras",
    "honduran": "honduras",
    # Guatemala
    "guatemala": "guatemala",
    "guatemalan": "guatemala",
    # El Salvador
    "el salvador": "el salvador",
    "salvadoran": "el salvador",
    # Nicaragua
    "nicaragua": "nicaragua",
    "nicaraguan": "nicaragua",
    # Panama
    "panama": "panama",
    "panamanian": "panama",
    # Dominican Republic
    "dominican republic": "dominican republic",
    "dominican": "dominican republic",
    # Puerto Rico
    "puerto rico": "puerto rico",
    "puerto rican": "puerto rico",
    # Netherlands
    "netherlands": "netherlands",
    "dutch": "netherlands",
    "holland": "netherlands",
    # Greece
    "greece": "greece",
    "greek": "greece",
    # Sweden
    "sweden": "sweden",
    "swedish": "sweden",
    # Norway
    "norway": "norway",
    "norwegian": "norway",
    # Denmark
    "denmark": "denmark",
    "danish": "denmark",
    # Finland
    "finland": "finland",
    "finnish": "finland",
    # Poland
    "poland": "poland",
    "polish": "poland",
    # Turkey
    "turkey": "turkey",
    "turkish": "turkey",
    # Saudi Arabia
    "saudi arabia": "saudi arabia",
    "saudi": "saudi arabia",
    # Egypt
    "egypt": "egypt",
    "egyptian": "egypt",
    # Morocco
    "morocco": "morocco",
    "moroccan": "morocco",
    # South Africa
    "south africa": "south africa",
    "south african": "south africa",
    # Nigeria
    "nigeria": "nigeria",
    "nigerian": "nigeria",
    # Ukraine
    "ukraine": "ukraine",
    "ukrainian": "ukraine",
    # Israel
    "israel": "israel",
    "israeli": "israel",
    # Iran
    "iran": "iran",
    "iranian": "iran",
    # Iraq
    "iraq": "iraq",
    "iraqi": "iraq",
    # Pakistan
    "pakistan": "pakistan",
    "pakistani": "pakistan",
    # Indonesia
    "indonesia": "indonesia",
    "indonesian": "indonesia",
    # Philippines
    "philippines": "philippines",
    "filipino": "philippines",
    "philippine": "philippines",
    # Thailand
    "thailand": "thailand",
    "thai": "thailand",
    # Vietnam
    "vietnam": "vietnam",
    "vietnamese": "vietnam",
    # New Zealand
    "new zealand": "new zealand",
    "new zealander": "new zealand",
    # Ireland
    "ireland": "ireland",
    "irish": "ireland",
    # Switzerland
    "switzerland": "switzerland",
    "swiss": "switzerland",
    # Belgium
    "belgium": "belgium",
    "belgian": "belgium",
    # Austria
    "austria": "austria",
    "austrian": "austria",
    # Czech Republic
    "czech republic": "czech republic",
    "czech": "czech republic",
    # Romania
    "romania": "romania",
    "romanian": "romania",
    # Hungary
    "hungary": "hungary",
    "hungarian": "hungary",
    # Serbia
    "serbia": "serbia",
    "serbian": "serbia",

    # ── Colours ───────────────────────────────────────────────────────
    "color": "artist palette",
    "colour": "artist palette",
    "red": "red circle",
    "blue": "blue circle",
    "green": "green circle",
    "yellow": "yellow circle",
    "orange": "orange circle",
    "purple": "purple circle",
    "brown": "brown circle",
    "black": "black circle",
    "white": "white circle",
    "pink": "pink heart",
    "grey": "white circle",
    "gray": "white circle",
    "gold": "coin",
    "silver": "coin",

    # ── Adjectives ────────────────────────────────────────────────────
    "expensive": "money with wings",
    "cheap": "label",
    "free": "label",
    "beautiful": "cherry blossom",
    "pretty": "cherry blossom",
    "ugly": "nauseated face",
    "fast": "high voltage",
    "quick": "high voltage",
    "slow": "snail",
    "hot": "hot face",
    "cold": "cold face",
    "freezing": "cold face",
    "warm": "sun with face",
    "new": "sparkles",
    "old": "older person",
    "ancient": "classical building",
    "big": "elephant",
    "large": "elephant",
    "huge": "elephant",
    "giant": "elephant",
    "small": "mouse face",
    "tiny": "mouse face",
    "little": "mouse face",
    "strong": "flexed biceps",
    "weak": "person shrugging",
    "rich": "money bag",
    "poor": "money with wings",
    "dark": "crescent moon",
    "bright": "sun",
    "wet": "water wave",
    "dry": "sun",
    "heavy": "weight lifter",
    "light": "feather",
    "loud": "speaker high volume",
    "quiet": "muted speaker",
    "broken": "broken heart",
    "lost": "world map",
    "found": "magnifying glass tilted left",
    "sharp": "kitchen knife",
    "clean": "soap",
    "dirty": "pile of poo",
    "full": "stuffed flatbread",
    "empty": "open hands",
    "tired": "sleeping face",
    "sick": "sneezing face",
    "healthy": "green apple",
    "dead": "skull",
    "alive": "sparkling heart",
    "open": "open hands",
    "closed": "locked",
    "hidden": "detective",
    "visible": "eye",
    "soft": "cloud",
    "hard": "rock",
    "smooth": "ribbon",
    "rough": "rock",
    "round": "red circle",
    "flat": "rolled-up newspaper",
    "straight": "straight ruler",
    "curved": "wavy dash",
}


def find_emoji(word: str) -> str | None:
    word = word.lower().strip()

    def _lookup(w):
        if w in emoji_lookup:
            return emoji_lookup[w]
        mapped = EMOJI_SYNONYMS.get(w)
        if mapped and mapped in emoji_lookup:
            return emoji_lookup[mapped]
        return None

    # 1. Direct match or manual synonym
    result = _lookup(word)
    if result:
        return result

    # 2. Singular form (apples -> apple)
    if word.endswith("s"):
        result = _lookup(word[:-1])
        if result:
            return result

    # 3. WordNet synonyms — automatically finds e.g. "cellular" -> "mobile phone"
    try:
        for syn in wordnet.synsets(word):
            for lemma in syn.lemmas():
                name = lemma.name().lower().replace("_", " ")
                result = _lookup(name)
                if result:
                    return result
    except Exception:
        pass

    return None


# ── Invidious fallback ────────────────────────────────────────────────────────

INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.privacyredirect.com",
    "https://yt.cdaut.de",
    "https://invidious.tiekoetter.com",
]


def _vtt_time(ts: str) -> float:
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return float(h) * 3600 + float(m) * 60 + float(s)


def parse_vtt(text: str) -> list[dict]:
    segments = []
    time_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
    )
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.strip().splitlines()
        start_s = end_s = None
        parts = []
        for line in lines:
            m = time_re.search(line)
            if m:
                start_s = _vtt_time(m.group(1))
                end_s = _vtt_time(m.group(2))
            elif start_s is not None and line and not line.strip().isdigit():
                clean = re.sub(r"<[^>]+>", "", line).strip()
                if clean:
                    parts.append(clean)
        if start_s is not None and parts:
            segments.append({
                "start": start_s,
                "duration": max(end_s - start_s, 0.1),
                "text": " ".join(parts),
            })
    return segments


async def fetch_subtitles_invidious(vid: str) -> list[dict] | None:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for instance in INVIDIOUS_INSTANCES:
            try:
                r = await client.get(f"{instance}/api/v1/captions/{vid}")
                if r.status_code != 200:
                    continue
                caps = r.json().get("captions", [])
                spanish = [c for c in caps if c.get("language_code", "").startswith("es")]
                if not spanish:
                    print(f"[DEBUG] Invidious {instance}: no Spanish captions", flush=True)
                    continue
                cap_url = spanish[0].get("url", "")
                if cap_url.startswith("/"):
                    cap_url = instance + cap_url
                vtt_r = await client.get(cap_url)
                if vtt_r.status_code != 200:
                    continue
                segs = parse_vtt(vtt_r.text)
                if segs:
                    print(f"[DEBUG] Invidious success via {instance}: {len(segs)} segments", flush=True)
                    return segs
            except Exception as exc:
                print(f"[DEBUG] Invidious {instance} failed: {exc}", flush=True)
    return None


@app.get("/api/version")
async def get_version():
    return {"version": VERSION}


@app.get("/api/debug/{vid}")
async def debug_subtitles(vid: str):
    """Diagnostic endpoint — shows exactly what yt-dlp finds for a video."""
    try:
        url = f"https://www.youtube.com/watch?v={vid}"
        ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True, **({"proxy": PROXY_URL} if PROXY_URL else {})}
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            None,
            lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False),
        )
        all_subs = {**info.get("automatic_captions", {}), **info.get("subtitles", {})}
        return {
            "title": info.get("title"),
            "available_langs": list(all_subs.keys()),
            "spanish_formats": {
                lang: [f.get("ext") for f in all_subs[lang]]
                for lang in all_subs if lang.startswith("es")
            },
        }
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)}


@app.post("/api/process")
async def process_video(req: VideoRequest):
    try:
        vid = extract_video_id(req.url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

    def event(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    async def generate():
        # Cached — stream result immediately with no progress events
        if vid in _cache:
            _cache.move_to_end(vid)
            yield event({"type": "result", **_cache[vid]})
            return

        # Step 1: fetch subtitles — try yt-dlp first, fall back to Invidious
        yield event({"type": "progress", "message": "Fetching subtitles…"})
        transcript = None

        try:
            url = f"https://www.youtube.com/watch?v={vid}"
            ydl_opts = {
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                **({"proxy": PROXY_URL} if PROXY_URL else {}),
            }
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None,
                lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False),
            )

            all_subs = {**info.get("automatic_captions", {}), **info.get("subtitles", {})}
            print(f"[DEBUG] yt-dlp langs: {list(all_subs.keys())}", flush=True)

            spanish_langs = ["es", "es-orig", "es-419", "es-ES", "es-MX", "es-AR", "es-CO", "es-US"]
            sub_url = None
            for lang in spanish_langs:
                for fmt in all_subs.get(lang, []):
                    if fmt.get("ext") == "json3":
                        sub_url = fmt["url"]
                        break
                if sub_url:
                    break

            if sub_url:
                async with httpx.AsyncClient() as client:
                    r = await client.get(sub_url, timeout=15.0)
                    r.raise_for_status()
                    sub_data = r.json()

                transcript = []
                for ev in sub_data.get("events", []):
                    segs = ev.get("segs", [])
                    text = "".join(s.get("utf8", "") for s in segs).strip()
                    if text and text != "\n":
                        transcript.append({
                            "text": text,
                            "start": ev["tStartMs"] / 1000,
                            "duration": ev.get("dDurationMs", 2000) / 1000,
                        })
                print(f"[DEBUG] yt-dlp transcript segments: {len(transcript)}", flush=True)
                if not transcript:
                    transcript = None
        except Exception as e:
            print(f"[DEBUG] yt-dlp failed: {type(e).__name__}: {e}", flush=True)

        if not transcript:
            yield event({"type": "progress", "message": "Trying alternative source…"})
            transcript = await fetch_subtitles_invidious(vid)

        if not transcript:
            yield event({"type": "result", "has_subtitles": False})
            return

        # Step 2: analyse level
        yield event({"type": "progress", "message": "Assessing language level…"})
        candidates = get_candidates(transcript)
        level = assess_level(transcript)
        yield event({"type": "level", "level": level})

        # Step 3: translate all words in parallel
        yield event({"type": "progress", "message": "Translating vocabulary…"})
        translator = GoogleTranslator(source="es", target="en")
        unique_words = list(dict.fromkeys(c["word"] for c in candidates if not c["word"].isdigit()))
        sem = asyncio.Semaphore(15)
        loop = asyncio.get_event_loop()

        async def translate_one(word):
            async with sem:
                try:
                    result = await loop.run_in_executor(None, translator.translate, word)
                    return (result or word).lower().strip()
                except Exception:
                    return word

        results = await asyncio.gather(*[translate_one(w) for w in unique_words])
        translation_map = dict(zip(unique_words, results))

        enriched = []
        for c in candidates:
            word = c["word"]
            eng = word if word.isdigit() else translation_map.get(word, word)

            # Numbers > 10: display as text (0–10 have keycap emojis)
            if eng.isdigit() and int(eng) > 10:
                enriched.append({**c, "search_term": eng, "number": eng})
                continue

            search = eng if eng in CONCRETE_WORDS else next(
                (w for w in eng.split() if w in CONCRETE_WORDS), None
            )
            if search:
                entry = {**c, "search_term": search}
                char = find_emoji(search)
                if char:
                    entry["emoji"] = char
                enriched.append(entry)

        result = {
            "has_subtitles": True,
            "video_id": vid,
            "level": level,
            "word_events": enriched,
        }

        _cache[vid] = result
        if len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)

        yield event({"type": "result", **result})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/image")
async def get_image(word: str):
    # 1. Emoji
    char = find_emoji(word)
    if char:
        return {"type": "emoji", "char": char}

    # 2. Pixabay illustration
    if PIXABAY_API_KEY:
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(
                    "https://pixabay.com/api/",
                    params={
                        "key": PIXABAY_API_KEY,
                        "q": word,
                        "image_type": "illustration",
                        "per_page": 3,
                        "safesearch": "true",
                    },
                    timeout=8.0,
                )
                hits = r.json().get("hits", [])
                if hits:
                    return {"type": "image", "url": hits[0]["webformatURL"]}
            except Exception:
                pass

    return {"type": "none"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")

"""App IDs of popular Samsung Tizen apps (2019+ IDs first). Used to build the
app list on firmware that no longer reports installed apps. IDs that are not
installed simply return 404 and are skipped."""

KNOWN_APPS: dict[str, str] = {
    "3201907018807": "Netflix",
    "11101200001": "Netflix",
    "111299001912": "YouTube",
    "3201910019365": "Prime Video",
    "3201512006785": "Prime Video",
    "3201901017640": "Disney+",
    "3201807016597": "Apple TV",
    "3201606009684": "Spotify",
    "3201601007625": "Hulu",
    "3202301029760": "Max",
    "3201601007230": "HBO Max",
    "3201512006963": "Plex",
    "3202203026841": "Twitch",
    "3201707014489": "YouTube TV",
    "3202006020991": "Peacock",
    "3201710014981": "Paramount+",
    "3201504001965": "Tubi",
    "3201808016802": "Pluto TV",
    "3201909019175": "Crunchyroll",
    "3201703012065": "Sling TV",
    "3201908019041": "Apple Music",
    "3201802015747": "Amazon Music",
    "3201804016111": "ESPN",
    "3202004020674": "Discovery+",
    "3201906018693": "Philo",
    "3201703012079": "Vudu",
    "org.tizen.browser": "Internet",
}

# Apps the TV also registers with its DIAL server (port 8080). DIAL answers
# "running or stopped" even on firmware where the REST app endpoints hang.
DIAL_NAMES: dict[str, str] = {
    "111299001912": "YouTube",
    "3201907018807": "Netflix",
    "11101200001": "Netflix",
}

# Other names people use for an app. Matching is by normalized name, so this
# only needs the ones that share no words with the real name.
APP_ALIASES: dict[str, str] = {
    "amazon": "Prime Video",
    "amazon prime": "Prime Video",
    "prime": "Prime Video",
    "disney": "Disney+",
    "disney plus": "Disney+",
    "hbo": "Max",
    "hbo max": "Max",
    "appletv": "Apple TV",
    "apple tv plus": "Apple TV",
    "paramount": "Paramount+",
    "paramount plus": "Paramount+",
    "browser": "Internet",
    "web browser": "Internet",
    "yt": "YouTube",
}

YOUTUBE_ID = "111299001912"

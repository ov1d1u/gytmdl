import datetime
import functools
import re
import shutil
import subprocess
from pathlib import Path

import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TYER, COMM, ULT, APIC, TXXX
from yt_dlp import YoutubeDL
from ytmusicapi import YTMusic

MP3_TAGS_MAP = {
    "album": TALB,
    "artist": TPE1,
    "comment": COMM,
    "lyrics": ULT,
    "release_date": TYER,
    "title": TIT2,
}


class Dl:
    def __init__(
        self,
        final_path: Path = None,
        temp_path: Path = None,
        cookies_location: Path = None,
        ffmpeg_location: str = None,
        itag: str = None,
        cover_size: int = None,
        cover_format: str = None,
        cover_quality: int = None,
        template_folder: str = None,
        template_file: str = None,
        exclude_tags: str = None,
        truncate: int = None,
        **kwargs,
    ):
        self.ytmusic = YTMusic()
        self.final_path = final_path
        self.temp_path = temp_path
        self.cookies_location = cookies_location
        self.ffmpeg_location = ffmpeg_location
        self.itag = itag
        self.cover_size = cover_size
        self.cover_format = cover_format
        self.cover_quality = cover_quality
        self.template_folder = template_folder
        self.template_file = template_file
        self.exclude_tags = (
            [i.lower() for i in exclude_tags.split(",")]
            if exclude_tags is not None
            else []
        )
        self.truncate = None if truncate is not None and truncate < 4 else truncate

    @functools.lru_cache()
    def get_ydl_extract_info(self, url):
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
        }
        if self.cookies_location is not None:
            ydl_opts["cookiefile"] = str(self.cookies_location)
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    def get_download_queue(self, url):
        url = url.split("&")[0]
        download_queue = []
        ydl_extract_info = self.get_ydl_extract_info(url)
        if "youtube" not in ydl_extract_info["webpage_url"]:
            raise Exception("Not a YouTube URL")
        if "MPREb_" in ydl_extract_info["webpage_url_basename"]:
            ydl_extract_info = self.get_ydl_extract_info(ydl_extract_info["url"])
        if "playlist" in ydl_extract_info["webpage_url_basename"]:
            download_queue.extend(ydl_extract_info["entries"])
        if "watch" in ydl_extract_info["webpage_url_basename"]:
            download_queue.append(ydl_extract_info)
        return download_queue

    def get_artist(self, artist_list):
        if len(artist_list) == 1:
            return artist_list[0]["name"]
        return (
            ", ".join([i["name"] for i in artist_list][:-1])
            + f' & {artist_list[-1]["name"]}'
        )

    def get_ytmusic_watch_playlist(self, video_id):
        ytmusic_watch_playlist = self.ytmusic.get_watch_playlist(video_id)
        if not ytmusic_watch_playlist["tracks"][0]["length"] and ytmusic_watch_playlist[
            "tracks"
        ][0].get("album"):
            raise Exception("Track is not available")
        if not ytmusic_watch_playlist["tracks"][0].get("album"):
            return None
        return ytmusic_watch_playlist

    def search_track(self, title):
        return self.ytmusic.search(title, "songs")[0]["videoId"]

    @functools.lru_cache()
    def get_ytmusic_album(self, browse_id):
        return self.ytmusic.get_album(browse_id)

    @functools.lru_cache()
    def get_cover(self, url):
        return requests.get(url).content

    def get_tags(self, ytmusic_watch_playlist):
        video_id = ytmusic_watch_playlist["tracks"][0]["videoId"]
        ytmusic_album = self.ytmusic.get_album(
            ytmusic_watch_playlist["tracks"][0]["album"]["id"]
        )
        tags = {
            "album": ytmusic_album["title"],
            "album_artist": self.get_artist(ytmusic_album["artists"]),
            "artist": self.get_artist(ytmusic_watch_playlist["tracks"][0]["artists"]),
            "comment": f"https://music.youtube.com/watch?v={video_id}",
            "cover_url": f'{ytmusic_watch_playlist["tracks"][0]["thumbnail"][0]["url"].split("=")[0]}'
            + f'=w{self.cover_size}-l{self.cover_quality}-{"rj" if self.cover_format == "jpg" else "rp"}',
            "media_type": 1,
            "title": ytmusic_watch_playlist["tracks"][0]["title"],
            "track_total": ytmusic_album["trackCount"],
        }
        for i, video in enumerate(
            self.get_ydl_extract_info(
                f'https://www.youtube.com/playlist?list={ytmusic_album["audioPlaylistId"]}'
            )["entries"]
        ):
            if video["id"] == video_id:
                try:
                    if ytmusic_album["tracks"][i]["isExplicit"]:
                        tags["rating"] = 1
                    else:
                        tags["rating"] = 0
                except IndexError:
                    tags["rating"] = 0
                finally:
                    tags["track"] = i + 1
                break
        if ytmusic_watch_playlist["lyrics"]:
            lyrics = self.ytmusic.get_lyrics(ytmusic_watch_playlist["lyrics"])["lyrics"]
            if lyrics is not None:
                tags["lyrics"] = lyrics
        if ytmusic_album.get("year"):
            tags["release_date"] = (
                datetime.datetime.strptime(ytmusic_album["year"], "%Y").isoformat()
                + "Z"
            )
            tags["release_year"] = ytmusic_album["year"]
        return tags

    def get_sanizated_string(self, dirty_string, is_folder):
        dirty_string = re.sub(r'[\\/:*?"<>|;]', "_", dirty_string)
        if is_folder:
            dirty_string = dirty_string[: self.truncate]
            if dirty_string.endswith("."):
                dirty_string = dirty_string[:-1] + "_"
        else:
            if self.truncate is not None:
                dirty_string = dirty_string[: self.truncate - 4]
        return dirty_string.strip()

    def get_temp_location(self, video_id):
        return self.temp_path / f"{video_id}.m4a"

    def get_fixed_location(self, video_id):
        return self.temp_path / f"{video_id}_fixed.mp3"

    def get_final_location(self, tags):
        final_location_folder = self.template_folder.split("/")
        final_location_file = self.template_file.split("/")
        final_location_folder = [
            self.get_sanizated_string(i.format(**tags), True)
            for i in final_location_folder
        ]
        final_location_file = [
            self.get_sanizated_string(i.format(**tags), True)
            for i in final_location_file[:-1]
        ] + [
            self.get_sanizated_string(final_location_file[-1].format(**tags), False)
            + ".mp3"
        ]
        return self.final_path.joinpath(*final_location_folder).joinpath(
            *final_location_file
        )

    def get_cover_location(self, final_location):
        return final_location.parent / f"Cover.{self.cover_format}"

    def download(self, video_id, temp_location):
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "fixup": "never",
            "format": self.itag,
            "outtmpl": str(temp_location),
        }
        if self.cookies_location is not None:
            ydl_opts["cookiefile"] = str(self.cookies_location)
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download("music.youtube.com/watch?v=" + video_id)

    def fixup(self, temp_location, fixed_location):
        fixup = [
            self.ffmpeg_location,
            "-loglevel",
            "error",
            "-i",
            temp_location,
        ]
        if self.itag == "251":
            fixup.extend(
                [
                    "-f",
                    "mp4",
                ]
            )
        subprocess.run(
            [
                *fixup,
                "-movflags",
                "+faststart",
                "-q:a",
                "2",
                fixed_location,
            ],
            check=True,
        )

    def apply_tags(self, fixed_location, tags):
        mp3_tags = [
            v(encoding=3, text=tags[k])
            for k, v in MP3_TAGS_MAP.items()
            if k not in self.exclude_tags and tags.get(k) is not None
        ]
        if not {"track", "track_total"} & set(self.exclude_tags):
            if "track" in tags and "track_total" in tags:
                mp3_tags.append(
                    TRCK(encoding=3, text=f"{tags['track']}/{tags['track_total']}")
                )
        if "cover" not in self.exclude_tags:
            mp3_tags.append(
                APIC(
                    encoding=3,
                    mime='image/jpeg' if self.cover_format == "jpg" else 'image/png',
                    type=3,
                    desc=u'Cover',
                    data=self.get_cover(tags["cover_url"])
                )
            )
        mp3 = MP3(fixed_location, ID3=ID3)
        if mp3.tags is None:
            mp3.add_tags()
        for tag in mp3_tags:
            mp3.tags.add(tag)
        mp3.tags.add(
            TXXX(encoding=3, desc="ytid", text=tags["ytid"])
        )
        mp3.save()

    def move_to_final_location(self, fixed_location, final_location):
        final_location.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(fixed_location, final_location)

    def save_cover(self, tags, cover_location):
        with open(cover_location, "wb") as f:
            f.write(self.get_cover(tags["cover_url"]))

    def cleanup(self):
        shutil.rmtree(self.temp_path)

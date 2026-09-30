"""Module to download a complete playlist from a youtube channel."""
import json
import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Tuple, Union, Any, Callable

from pytubefix import extract, request, YouTube
from pytubefix.innertube import InnerTube
from pytubefix.helpers import cache, DeferredGeneratorList, install_proxy, uniqueify

logger = logging.getLogger(__name__)


class Playlist(Sequence):
    """Load a YouTube playlist with URL"""

    def __init__(
            self,
            url: str,
            client: str = InnerTube().client_name,
            proxies: Optional[Dict[str, str]] = None,
            use_oauth: bool = False,
            allow_oauth_cache: bool = True,
            token_file: Optional[str] = None,
            oauth_verifier: Optional[Callable[[str, str], None]] = None,
            use_po_token: Optional[bool] = False,
            po_token_verifier: Optional[Callable[[None], Tuple[str, str]]] = None,
    ):
        """
        :param dict proxies:
            (Optional) A dict mapping protocol to proxy address which will be used by pytube.
        :param bool use_oauth:
            (Optional) Prompt the user to authenticate to YouTube.
            If allow_oauth_cache is set to True, the user should only be prompted once.
        :param bool allow_oauth_cache:
            (Optional) Cache OAuth tokens locally on the machine. Defaults to True.
            These tokens are only generated if use_oauth is set to True as well.
        :param str token_file:
            (Optional) Path to the file where the OAuth tokens will be stored.
            Defaults to None, which means the tokens will be stored in the pytubefix/__cache__ directory.
        :param Callable oauth_verifier:
            (optional) Verifier to be used for getting OAuth tokens. 
            Verification URL and User-Code will be passed to it respectively.
            (if passed, else default verifier will be used)
        :param bool use_po_token:
            (Optional) Prompt the user to use the proof of origin token on YouTube.
            It must be sent with the API along with the linked visitorData and
            then passed as a `po_token` query parameter to affected clients.
            If allow_oauth_cache is set to True, the user should only be prompted once.
        :param Callable po_token_verifier:
            (Optional) Verifier used to obtain the visitorData and po_token.
            The verifier will return the visitorData and po_token respectively.
            (if passed, else default verifier will be used)
        """
        if proxies:
            install_proxy(proxies)

        self._input_url = url
        self._visitor_data = None

        self.client = client
        self.use_oauth = use_oauth
        self.allow_oauth_cache = allow_oauth_cache
        self.token_file = token_file
        self.oauth_verifier = oauth_verifier

        self.use_po_token = use_po_token
        self.po_token_verifier = po_token_verifier

        # These need to be initialized as None for the properties.
        self._html = None
        self._ytcfg = None
        self._initial_data = None
        self._sidebar_info = None

        self._playlist_id = None

    @property
    def playlist_id(self):
        """Get the playlist id.

        :rtype: str
        """
        if self._playlist_id:
            return self._playlist_id
        self._playlist_id = extract.playlist_id(self._input_url)
        return self._playlist_id

    @property
    def playlist_url(self):
        """Get the base playlist url.

        :rtype: str
        """
        return f"https://www.youtube.com/playlist?list={self.playlist_id}"

    @property
    def html(self):
        """Get the playlist page html.

        :rtype: str
        """
        if self._html:
            return self._html
        self._html = request.get(self.playlist_url)
        return self._html

    @property
    def ytcfg(self):
        """Extract the ytcfg from the playlist page html.

        :rtype: dict
        """
        if self._ytcfg:
            return self._ytcfg
        self._ytcfg = extract.get_ytcfg(self.html)
        return self._ytcfg

    @property
    def initial_data(self):
        """Extract the initial data from the playlist page html.

        :rtype: dict
        """
        if self._initial_data:
            return self._initial_data
        else:
            self._initial_data = extract.initial_data(self.html)
            return self._initial_data

    @property
    def sidebar_info(self):
        """Extract the sidebar info from the playlist page html.

        :rtype: dict
        """
        if self._sidebar_info:
            return self._sidebar_info
        else:
            self._sidebar_info = self.initial_data['sidebar'][
                'playlistSidebarRenderer']['items']
            return self._sidebar_info

    @property
    def yt_api_key(self):
        """Extract the INNERTUBE_API_KEY from the playlist ytcfg.

        :rtype: str
        """
        return self.ytcfg['INNERTUBE_API_KEY']

    def _paginate(
            self, initial_html: str, context: Optional[Any] = None,
            until_watch_id: Optional[str] = None
    ) -> Iterable[List[str]]:
        """Parse the video links from the page source, yields the /watch?v=
        part from video link

        :param initial_html str: html from the initial YouTube url, default: self.html
        :param context Optional[Any]: Auxiliary object
        :param until_watch_id Optional[str]: YouTube Video watch id until
            which the playlist should be read.

        :rtype: Iterable[List[str]]
        :returns: Iterable of lists of YouTube watch ids
        """
        videos_urls, continuation = self._extract_videos(
            json.dumps(extract.initial_data(initial_html)), context
        )
        seen_urls = list(videos_urls)
        if until_watch_id:
            try:
                trim_index = videos_urls.index(f"/watch?v={until_watch_id}")
                yield videos_urls[:trim_index]
                return
            except ValueError:
                pass
        yield videos_urls

        if (
                seen_urls
                and isinstance(seen_urls[0], str)
                and len(uniqueify(seen_urls)) >= 100
        ):
            extra_urls = self._extract_watch_panel_video_urls(seen_urls[0])
            unseen_urls = [url for url in extra_urls if url not in seen_urls]
            if unseen_urls:
                if until_watch_id:
                    try:
                        trim_index = unseen_urls.index(f"/watch?v={until_watch_id}")
                        yield unseen_urls[:trim_index]
                        return
                    except ValueError:
                        pass
                yield unseen_urls
                return

        # Extraction from a playlist only returns 100 videos at a time
        # if self._extract_videos returns a continuation there are more
        # than 100 songs inside a playlist, so we need to add further requests
        # to gather all of them

        while continuation:  # there is an url found
            # requesting the next page of videos with the url generated from the
            # previous page, needs to be a post
            req = InnerTube('WEB').browse(continuation=continuation, visitor_data=self._visitor_data)
            # extract up to 100 songs from the page loaded
            # returns another continuation if more videos are available
            videos_urls, continuation = self._extract_videos(req, context)
            seen_urls.extend(videos_urls)
            if until_watch_id:
                try:
                    trim_index = videos_urls.index(f"/watch?v={until_watch_id}")
                    yield videos_urls[:trim_index]
                    return
                except ValueError:
                    pass
            yield videos_urls

        if (
                seen_urls
                and isinstance(seen_urls[0], str)
                and len(uniqueify(seen_urls)) >= 200
        ):
            extra_urls = self._extract_watch_panel_video_urls(seen_urls[0])
            unseen_urls = [url for url in extra_urls if url not in seen_urls]
            if until_watch_id:
                try:
                    trim_index = unseen_urls.index(f"/watch?v={until_watch_id}")
                    yield unseen_urls[:trim_index]
                    return
                except ValueError:
                    pass
            if unseen_urls:
                yield unseen_urls

    def _extract_watch_panel_video_urls(self, seed_watch_url: str) -> List[str]:
        """Extract long playlists from the watch-page playlist panel.

        YouTube's playlist browse endpoint can stop returning continuation
        tokens after 200 lockupViewModel items. The watch next endpoint still
        exposes wider playlist-panel windows when seeded with later videos.
        """
        try:
            selected_video_id = seed_watch_url.split("v=", 1)[1].split("&", 1)[0]
        except IndexError:
            return []

        collected_ids = []
        selected_ids = set()
        for _ in range(30):
            if not selected_video_id or selected_video_id in selected_ids:
                break
            selected_ids.add(selected_video_id)

            client = InnerTube('WEB')
            client.base_data.update({
                "videoId": selected_video_id,
                "playlistId": self.playlist_id,
                "contentCheckOk": True,
                "racyCheckOk": True,
            })
            response = client._call_api(
                f"{client.base_url}/next",
                client.base_params,
                client.base_data
            )
            panel_ids = self._extract_playlist_panel_video_ids(response)
            if not panel_ids:
                break

            previous_count = len(collected_ids)
            collected_ids = uniqueify(collected_ids + panel_ids)
            if len(collected_ids) == previous_count:
                break

            selected_video_id = collected_ids[max(0, len(collected_ids) - 101)]

        return [f"/watch?v={video_id}" for video_id in collected_ids]

    def _extract_playlist_panel_video_ids(self, raw_json: Dict) -> List[str]:
        """Extract video ids from watch-page playlistPanelVideoRenderer items."""
        video_ids = []
        for renderer in self._find_dict_values(raw_json, 'playlistPanelVideoRenderer'):
            if isinstance(renderer, dict) and renderer.get('videoId'):
                video_ids.append(renderer['videoId'])
        return uniqueify(video_ids)

    def _find_dict_values(self, data: Any, key: str) -> Iterable[Any]:
        """Yield values for a key from a nested dict/list response."""
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if key in current:
                    yield current[key]
                stack.extend(reversed(list(current.values())))
            elif isinstance(current, list):
                stack.extend(reversed(current))

    def _extract_videos(self, raw_json: str, context: Optional[Any] = None) -> Tuple[List[str], Optional[str]]:
        """Extracts videos from a raw json page

        :param str raw_json: Input json extracted from the page or the last
            server response
        :param Optional[Any] context: Auxiliary object from _paginate
        :rtype: Tuple[List[str], Optional[str]]
        :returns: Tuple containing a list of up to 100 video watch ids and
            a continuation token, if more videos are available
        """
        if isinstance(raw_json, dict):
            initial_data = raw_json
        else:
            initial_data = json.loads(raw_json)
        try:
            # this is the json tree structure, if the json was extracted from
            # html
            section_contents = initial_data["contents"][
                "twoColumnBrowseResultsRenderer"][
                "tabs"][0]["tabRenderer"]["content"][
                "sectionListRenderer"]["contents"]
            try:
                item_section_contents = section_contents[0]["itemSectionRenderer"]["contents"]
                renderer = item_section_contents[0]

                if 'richGridRenderer' in renderer:
                    important_content = renderer["richGridRenderer"]
                elif 'lockupViewModel' in renderer:
                    videos = list(item_section_contents)
                    important_content = None
                else:
                    important_content = renderer["playlistVideoListRenderer"]

            except (KeyError, IndexError, TypeError):
                # Playlist with submenus
                important_content = section_contents[
                    1]["itemSectionRenderer"][
                    "contents"][0]["playlistVideoListRenderer"]
            if important_content is not None:
                videos = important_content["contents"]

            try:
                self._visitor_data = initial_data["responseContext"]["webResponseContextExtensionData"][
                    "ytConfigData"]["visitorData"]
            except (KeyError, TypeError):
                pass
        except (KeyError, IndexError, TypeError):
            try:
                # this is the json tree structure, if the json was directly sent
                # by the server in a continuation response
                # no longer a list and no longer has the "response" key
                important_content = initial_data['onResponseReceivedActions'][0][
                    'appendContinuationItemsAction']['continuationItems']
                videos = []
                for item in important_content:
                    if 'itemSectionRenderer' in item:
                        videos.extend(item['itemSectionRenderer'].get('contents', []))
                    else:
                        videos.append(item)
            except (KeyError, IndexError, TypeError) as p:
                logger.info(p)
                return [], None

        try:
            # For some reason YouTube only returns the first 100 shorts of a playlist
            # token provided by the API doesn't seem to work even in the official player
            try:
                continuation = videos[-1]['continuationItemRenderer']['continuationEndpoint']['continuationCommand']['token']
            except (KeyError, IndexError, TypeError):
                try:
                    continuation = videos[-1]['continuationItemViewModel'][
                        'continuationCommand']['innertubeCommand'][
                        'continuationCommand']['token']
                except (KeyError, IndexError, TypeError):
                    for command in videos[-1]['continuationItemRenderer'][
                        'continuationEndpoint']['commandExecutorCommand']['commands']:
                        if 'continuationCommand' in command:
                            continuation = command['continuationCommand']['token']
                            break
            videos = videos[:-1]
        except (KeyError, IndexError):
            # if there is an error, no continuation is available
            continuation = None

        items_obj = self._extract_ids(videos)

        # remove duplicates
        return uniqueify(items_obj), continuation

    def _extract_ids(self, items: list) -> list:
        """ Iterate over the extracted urls.

        :returns: List with extracted ids.
        """
        items_obj = []
        for x in items:
            extracted_item = self._extract_video_id(x)
            if extracted_item:
                items_obj.append(extracted_item)
        return items_obj

    def _extract_video_id(self, x: dict):
        """ Try extracting video ids, if it fails, try extracting shorts ids.

        :returns: List with extracted ids.
        """
        try:
            return f"/watch?v={x['playlistVideoRenderer']['videoId']}"
        except (KeyError, IndexError, TypeError):
            try:
                return self._extract_lockup_video_id(x)
            except (KeyError, IndexError, TypeError):
                return self._extract_shorts_id(x)

    def _extract_lockup_video_id(self, x: dict):
        """Try extracting video ids from YouTube's lockupViewModel playlist items."""
        lockup = x['lockupViewModel']
        if lockup.get('contentType') != 'LOCKUP_CONTENT_TYPE_VIDEO':
            return []

        video_id = lockup.get('contentId')
        if not video_id:
            video_id = lockup['rendererContext']['commandContext']['onTap'][
                'innertubeCommand']['watchEndpoint']['videoId']

        return f"/watch?v={video_id}"

    def _extract_shorts_id(self, x: dict):
        """ Try extracting shorts ids.

        :returns: List with extracted ids.
        """
        try:
            content = x['richItemRenderer']['content']

            # New json tree added on 09/12/2024
            if 'shortsLockupViewModel' in content:
                video_id = content['shortsLockupViewModel']['onTap']['innertubeCommand']['reelWatchEndpoint']['videoId']
            else:
                video_id = content['reelItemRenderer']['videoId']

            return f"/watch?v={video_id}"

        except (KeyError, IndexError, TypeError):
            return []

    def trimmed(self, video_id: str) -> Iterable[str]:
        """Retrieve a list of YouTube video URLs trimmed at the given video ID

        i.e. if the playlist has video IDs 1,2,3,4 calling trimmed(3) returns
        [1,2]
        :type video_id: str
            video ID to trim the returned list of playlist URLs at
        :rtype: List[str]
        :returns:
            List of video URLs from the playlist trimmed at the given ID
        """
        for page in self._paginate(self.html, until_watch_id=video_id):
            yield from (self._video_url(watch_path) for watch_path in page)

    def url_generator(self):
        """Generator that yields video URLs.

        :Yields: Video URLs
        """
        for page in self._paginate(self.html):
            for video in page:
                yield self._video_url(video)

    @property  # type: ignore
    @cache
    def video_urls(self) -> DeferredGeneratorList:
        """Complete links of all the videos in playlist

        :rtype: List[str]
        :returns: List of video URLs
        """
        return DeferredGeneratorList(self.url_generator())

    def videos_generator(self):
        for url in self.video_urls:
            yield YouTube(
                url,
                client=self.client,
                use_oauth=self.use_oauth,
                allow_oauth_cache=self.allow_oauth_cache,
                token_file=self.token_file,
                oauth_verifier=self.oauth_verifier,
                use_po_token=self.use_po_token,
                po_token_verifier=self.po_token_verifier
            )

    @property
    def videos(self) -> Iterable[YouTube]:
        """Yields YouTube objects of videos in this playlist

        :rtype: List[YouTube]
        :returns: List of YouTube
        """
        return DeferredGeneratorList(self.videos_generator())

    def __getitem__(self, i: Union[slice, int]) -> Union[str, List[str]]:
        return self.video_urls[i]

    def __len__(self) -> int:
        return len(self.video_urls)

    def __repr__(self) -> str:
        return f'<pytubefix.contrib.Playlist object: playlistId={self.playlist_id}>'

    @property
    @cache
    def last_updated(self) -> Optional[date]:
        """Extract the date that the playlist was last updated.

        For some playlists, this will be a specific date, which is returned as a datetime
        object. For other playlists, this is an estimate such as "1 week ago". Due to the
        fact that this value is returned as a string, pytube does a best-effort parsing
        where possible, and returns the raw string where it is not possible.

        :return: Date of last playlist update where possible, else the string provided
        :rtype: datetime.date
        """
        last_updated_text = self.sidebar_info[0]['playlistSidebarPrimaryInfoRenderer'][
            'stats'][2]['runs'][1]['text']
        try:
            date_components = last_updated_text.split()
            month = date_components[0]
            day = date_components[1].strip(',')
            year = date_components[2]
            return datetime.strptime(
                f"{month} {day:0>2} {year}", "%b %d %Y"
            ).date()
        except (IndexError, KeyError):
            return last_updated_text

    @property
    @cache
    def title(self) -> Optional[str]:
        """Extract playlist title

        :return: playlist title (name)
        :rtype: Optional[str]
        """
        return self.sidebar_info[0]['playlistSidebarPrimaryInfoRenderer'][
            'title']['runs'][0]['text']

    @property
    def thumbnail_url(self):
        thumbnail_renderer = self.sidebar_info[0][
                'playlistSidebarPrimaryInfoRenderer'][
                'thumbnailRenderer']

        if 'playlistVideoThumbnailRenderer' in thumbnail_renderer:
            return thumbnail_renderer[
                'playlistVideoThumbnailRenderer'][
                'thumbnail'][
                'thumbnails'][-1][
                'url']

        elif 'playlistCustomThumbnailRenderer' in thumbnail_renderer:
            return thumbnail_renderer[
                'playlistCustomThumbnailRenderer'][
                'thumbnail'][
                'thumbnails'][-1][
                'url']

    @property
    def description(self) -> str:
        return self.sidebar_info[0]['playlistSidebarPrimaryInfoRenderer'][
            'description']['simpleText']

    @property
    def length(self):
        """Extract the number of videos in the playlist.

        :return: Playlist video count
        :rtype: int
        """
        count_text = self.sidebar_info[0]['playlistSidebarPrimaryInfoRenderer'][
            'stats'][0]['runs'][0]['text']
        count_text = count_text.replace(',', '')
        return int(count_text)

    @property
    def views(self):
        """Extract view count for playlist.

        :return: Playlist view count
        :rtype: int
        """
        # "1,234,567 views"
        views_text = self.sidebar_info[0]['playlistSidebarPrimaryInfoRenderer'][
            'stats'][1]['simpleText']
        # "1,234,567"
        count_text = views_text.split()[0]
        # "1234567"
        count_text = count_text.replace(',', '')
        return int(count_text)

    @property
    def owner(self):
        """Extract the owner of the playlist.

        :return: Playlist owner name.
        :rtype: str
        """
        return self.sidebar_info[1]['playlistSidebarSecondaryInfoRenderer'][
            'videoOwner']['videoOwnerRenderer']['title']['runs'][0]['text']

    @property
    def owner_id(self):
        """Extract the channel_id of the owner of the playlist.

        :return: Playlist owner's channel ID.
        :rtype: str
        """
        return self.sidebar_info[1]['playlistSidebarSecondaryInfoRenderer'][
            'videoOwner']['videoOwnerRenderer']['title']['runs'][0][
            'navigationEndpoint']['browseEndpoint']['browseId']

    @property
    def owner_url(self):
        """Create the channel url of the owner of the playlist.

        :return: Playlist owner's channel url.
        :rtype: str
        """
        return f'https://www.youtube.com/channel/{self.owner_id}'

    @staticmethod
    def _video_url(watch_path: str):
        return f"https://www.youtube.com{watch_path}"

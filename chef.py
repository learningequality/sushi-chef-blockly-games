#!/usr/bin/env python

"""
Sushi Chef for https://blockly-games.appspot.com
Learn programming using drag-and-drop blocks.
We make HTML5 apps out of the 7 games.

Note: There are actually offline archives available here:
https://github.com/google/blockly-games/wiki/Offline
However these are missing some icons and sounds.
(Also this chef was written before I found out about those!)
"""

from collections import defaultdict
import html
import os
import re
import requests
import tempfile
import time
from urllib.parse import urlparse, parse_qs
import uuid

from bs4 import BeautifulSoup

import le_utils.constants
from ricecooker.chefs import SushiChef
from ricecooker.classes import nodes, files, licenses
from ricecooker.utils.caching import CacheForeverHeuristic, FileCache, CacheControlAdapter, InvalidatingCacheControlAdapter
from ricecooker.utils.browser import preview_in_browser
from ricecooker.utils.html import download_file, WebDriver
from ricecooker.utils.zip import create_predictable_zip
import selenium.webdriver.support.ui as selenium_ui
from distutils.dir_util import copy_tree


try:
    import secrets
    _GITHUB_API_TOKEN = getattr(secrets, 'GITHUB_PERSONAL_ACCESS_TOKEN', None)
except ImportError:
    _GITHUB_API_TOKEN = None


_NO_GITHUB_API_TOKEN_MESSAGE = """
*******************************************************************************
* Warning: GitHub API token not found in secrets.py. Scraping might be
* rate-limited. See secrets.py.example to obtain a GitHub personal access token
*******************************************************************************
"""
if not _GITHUB_API_TOKEN:
    print(_NO_GITHUB_API_TOKEN_MESSAGE)

sess = requests.Session()
cache = FileCache('.webcache')
forever_adapter = CacheControlAdapter(heuristic=CacheForeverHeuristic(), cache=cache)

sess.mount('https://blockly-games.appspot.com', forever_adapter)
sess.mount('http://fonts.googleapis.com', forever_adapter)
sess.mount('https://raw.githubusercontent.com', forever_adapter)
sess.mount('https://api.github.com', forever_adapter)


headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:20.0) Gecko/20100101 Firefox/20.0",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive"
}


class BlocklyGamesChef(SushiChef):
    """
    The chef class that takes care of uploading channel to the content curation server.

    We'll call its `main()` method from the command line script.
    """
    channel_info = {
        'CHANNEL_SOURCE_DOMAIN': "blockly-games.appspot.com",
        'CHANNEL_SOURCE_ID': "blockly-games",
        'CHANNEL_TITLE': "Blockly Games",
        'CHANNEL_THUMBNAIL': "thumbnail.png",
        'CHANNEL_DESCRIPTION': "Teaches programming using block-based games, for children who have no prior experience with programming.",
    }

    def construct_channel(self, **kwargs):
        """
        Create ChannelNode and build topic tree.
        """
        # create channel
        channel_info = self.channel_info
        channel = nodes.ChannelNode(
            source_domain = channel_info['CHANNEL_SOURCE_DOMAIN'],
            source_id = channel_info['CHANNEL_SOURCE_ID'],
            title = channel_info['CHANNEL_TITLE'],
            thumbnail = channel_info.get('CHANNEL_THUMBNAIL'),
            description = channel_info.get('CHANNEL_DESCRIPTION'),
            language = "en",
        )

        download_all_languages(channel)
        return channel


def download_all_languages(channel):
    """Download all available languages from Blockly."""
    languages = []

    with WebDriver("https://blockly-games.appspot.com", delay=1000) as driver:
        for option in driver.find_elements_by_css_selector('#languageMenu option'):
            blockly_language_code = option.get_attribute('value')
            language_title = option.text
            le_language_code = blockly_language_code

            # There are some obscure languages that Blockly supports that we
            # don't yet know about in le-utils. Skip those for now.
            # TODO(davidhu): Add these language codes to le-utils
            if le_language_code in ['hrx', 'pms', 'sco', 'be-tarask']:
                continue

            # ... and sometimes we do know about the language but our language
            # code is different than Blockly's.
            # TODO(davidhu): Add these special cases to le-utils
            if le_language_code == 'pt-br':
                le_language_code = 'pt-BR'
            elif le_language_code == 'zh-hant':
                le_language_code = 'zh-TW'
            elif le_language_code == 'zh-hans':
                le_language_code = 'zh-CN'

            topic = nodes.TopicNode(
                source_id=le_language_code,
                title=language_title,
                language=le_language_code,
            )
            languages.append((topic, blockly_language_code, le_language_code))

    for topic, blockly_language_code, le_language_code in languages:
        print('Downloading puzzles for language %s (from https://blockly-games.appspot.com/?lang=%s)' % (
            topic.title, blockly_language_code))
        download_puzzles_for_language(topic, blockly_language_code, le_language_code)
        channel.add_child(topic)


def download_puzzles_for_language(topic_node, blockly_language_code, le_language_code):
    """Download all puzzles given for a given language."""
    puzzles = []
    descriptions = []

    with WebDriver("https://blockly-games.appspot.com/?lang=%s" % blockly_language_code, delay=1000) as driver:
        # Fetch puzzle metadata
        for i, icon in enumerate(driver.find_elements_by_css_selector('.icon')):
            title = icon.find_element_by_css_selector('text').text
            image_src = icon.find_element_by_css_selector('image').get_attribute('xlink:href')
            thumbnail = make_fully_qualified_url(image_src)
            puzzle_url = icon.find_element_by_css_selector('a').get_attribute('xlink:href')

            # For some reason Selenium always gives us an empty string for the
            # title of the last puzzle, even though it's there in the HTML, so
            # we're just going to grab it from the translations JSON file on
            # GitHub.
            if puzzle_url.split('?')[0] == 'pond-duck':
                github_url = ('https://raw.githubusercontent.com/google/blockly-games/master/json/%s.json' %
                        blockly_language_code.lower())
                response_json = make_request(github_url).json()
                title = response_json.get('Games.pond', 'Pond')

            puzzles.append((title, thumbnail, puzzle_url))

        # Fetch puzzle descriptions
        driver.get('https://blockly-games.appspot.com/about?lang=%s' % blockly_language_code)
        for tr in driver.find_elements_by_css_selector('table tr'):
            descriptions.append(tr.text)

    for (title, thumbnail, puzzle_url), description in zip(puzzles, descriptions):
        print('    Downloading puzzle "%s": %s (from https://blockly-games.appspot.com/%s)' %
                (title, description, puzzle_url))
        topic_node.add_child(download_puzzle(
                puzzle_url, title, description, thumbnail, le_language_code,
                blockly_language_code))


def download_puzzle(puzzle_url, title, description, thumbnail,
        le_language_code, blockly_language_code):
    """Download a single puzzle and return an HTML5 app node."""
    with WebDriver("https://blockly-games.appspot.com/%s" % puzzle_url, delay=1000) as driver:
        doc = BeautifulSoup(driver.page_source, "html.parser")

    # Create a temporary folder to download all the files for a puzzle.
    destination = tempfile.mkdtemp()

    # Download all the JS/CSS/images/audio/etc we can get from scraping the
    # page source.
    doc = download_static_assets(doc, destination)

    # Download other files not picked up by the above generic assets fetching,
    # e.g. from GitHub.
    puzzle_name = puzzle_url.split('?')[0]
    download_additional_assets(destination, puzzle_name)

    # Make some modifications to the HTML source -- hide some elements.
    remove_node(doc, '#languageMenu')
    remove_node(doc, '#title')

    # Copy over some of our own JS/CSS files and then add links to them in the
    # page source.
    copy_tree("static", os.path.join(destination, "static"))

    chef_body_script = doc.new_tag("script", src="static/chef_end_of_body.js")
    doc.select_one('body').append(chef_body_script)

    chef_head_script = doc.new_tag("script")
    chef_head_script.string = 'window["BlocklyGamesLang"] = "%s";' % blockly_language_code
    doc.select_one('head').insert(0, chef_head_script)

    # Write out the HTML source.
    with open(os.path.join(destination, "index.html"), "w") as f:
        f.write(str(doc))

    print("    Downloaded puzzle %s titled \"%s\" (thumbnail %s) to destination %s" % (
        puzzle_url, title, thumbnail, destination))
    #preview_in_browser(destination)

    zip_path = create_predictable_zip(destination)
    return nodes.HTML5AppNode(
        source_id=puzzle_url,
        title=truncate_metadata(title),
        description=description,
        license=licenses.PublicDomainLicense(copyright_holder='Google'),
        thumbnail=thumbnail,
        files=[files.HTMLZipFile(zip_path)],
        language=le_language_code,
    )


def download_additional_assets(destination, puzzle_name):
    url = make_fully_qualified_url('/third-party/JS-Interpreter/compiled.js')
    download_file(url, os.path.join(destination, 'third-party/JS-Interpreter'),
            request_fn=make_request, filename='compiled.js')

    dir_name = puzzle_name
    if dir_name == 'pond-tutor' or dir_name == 'pond-duck':
        dir_name = 'pond'

        url = make_fully_qualified_url('/pond/docs/generated/en/compressed.js')
        download_file(url, os.path.join(destination, 'pond/docs/generated/en'),
                request_fn=make_request, filename='compressed.js')

        url = make_fully_qualified_url('third-party/ace/worker-javascript.js')
        download_file(url, destination, request_fn=make_request, filename='worker-javascript.js')

        download_assets_from_github(
                'google/blockly-games',
                'appengine/pond/docs',
                os.path.join(destination, 'pond/docs'))

    download_assets_from_github(
            'google/blockly-games',
            'appengine/%s' % dir_name,
            os.path.join(destination, dir_name))
    download_assets_from_github('google/blockly-games',
            'appengine/%s' % dir_name,
            destination)
    download_assets_from_github('google/blockly-games',
            'appengine/common', os.path.join(destination, 'common'))
    download_assets_from_github('google/blockly', 'media', destination)
    download_assets_from_github('google/blockly', 'media',
            os.path.join(destination, 'third-party/blockly/media'))


def download_assets_from_github(repo_name, repo_path, destination):
    print('        Downloading files from GitHub repo %s/%s ...' % (
        repo_name, repo_path))

    access_token_param = ''
    if _GITHUB_API_TOKEN:
        access_token_param = '&access_token=%s' % _GITHUB_API_TOKEN

    url = 'https://api.github.com/repos/%s/contents/%s?ref=master%s' % (
            repo_name, repo_path, access_token_param)
    response = make_request(url)

    for item in response.json():
        if item['type'] == 'file':
            filename = item['name']
            download_url = item['download_url']
            print('        Downloading %s' % download_url)
            download_file(download_url, destination, request_fn=make_request,
                    filename=filename)


def remove_node(doc, selector):
    node = doc.select_one(selector)
    if node:
        node.decompose()


def truncate_metadata(data_string):
    MAX_CHARS = 190
    if len(data_string) > MAX_CHARS:
        data_string = data_string[:190] + " ..."
    return data_string


CSS_URL_RE = re.compile(r"url\(['\"]?(.*?)['\"]?\)")
IMAGES_IN_JS_RE = re.compile(r"images/(.*?)['\")]")


# TODO(davidhu): Much of this is copied from 3asafeer Sushi Chef and may be
# re-useable for other HTML5 apps. Split this out into a chef util.
def download_static_assets(doc, destination):
    """Download all the static assets for a given book's HTML soup.

    Will download JS, CSS, images, and audio clips.
    """
    # Helper function to download all assets for a given CSS selector.
    def download_assets(selector, attr, url_middleware=None,
            content_middleware=None, node_filter=None):
        nodes = doc.select(selector)

        for i, node in enumerate(nodes):

            if node_filter:
                if not node_filter(node):
                    src = node[attr]
                    node[attr] = ''
                    print('        Skipping node with src ', src)
                    continue

            url = make_fully_qualified_url(node[attr])

            if is_blacklisted(url):
                print('        Skipping downloading blacklisted url', url)
                node[attr] = ""
                continue

            if url_middleware:
                url = url_middleware(url)

            filename = derive_filename(url)
            node[attr] = filename

            print("        Downloading", url, "to filename", filename)
            download_file(url, destination, request_fn=make_request,
                    filename=filename, middleware_callbacks=content_middleware)

    def js_middleware(content, url, **kwargs):
        # Download all images referenced in JS files
        for img in IMAGES_IN_JS_RE.findall(content):
            url = make_fully_qualified_url('/images/%s' % img)
            print("        Downloading", url, "to filename", img)
            download_file(url, destination, subpath="images",
                    request_fn=make_request, filename=img)

        # Polyfill localStorage and document.cookie as iframes can't access
        # them
        return (content
            .replace("localStorage", "_localStorage")
            .replace('document.cookie.split', '"".split')
            .replace('document.cookie', 'window._document_cookie'))

    def css_node_filter(node):
        return "stylesheet" in node["rel"]

    def css_content_middleware(content, url, **kwargs):
        file_dir = os.path.dirname(urlparse(url).path)

        # Download linked fonts and images
        def repl(match):
            src = match.group(1)
            if src.startswith('//localhost'):
                return 'url()'
            # Don't download data: files
            if src.startswith('data:'):
                return match.group(0)
            src_url = make_fully_qualified_url(os.path.join(file_dir, src))
            derived_filename = derive_filename(src_url)
            download_file(src_url, destination, request_fn=make_request,
                    filename=derived_filename)
            return 'url("%s")' % derived_filename

        return CSS_URL_RE.sub(repl, content)

    # Download all linked static assets.
    download_assets("img[src]", "src")  # Images
    download_assets("link[href]", "href",
            content_middleware=css_content_middleware,
            node_filter=css_node_filter)  # CSS
    download_assets("script[src]", "src", content_middleware=js_middleware) # JS
    download_assets("source[src]", "src") # Audio
    download_assets("source[srcset]", "srcset") # Audio

    # ... and also run the middleware on CSS/JS embedded in the page source to
    # get linked files.
    for node in doc.select('style'):
        node.string = css_content_middleware(node.get_text(), url='')

    for node in doc.select('script'):
        if not node.attrs.get('src'):
            node.string = js_middleware(node.get_text(), url='')

    return doc


url_blacklist = [
    'analytics.js',
]

def is_blacklisted(url):
    return any((item in url) for item in url_blacklist)


def derive_filename(url):
    return "%s.%s" % (uuid.uuid4().hex, os.path.basename(urlparse(url).path))


class Dummy404ResponseObject(requests.Response):
    def __init__(self, url):
        super(Dummy404ResponseObject, self).__init__()
        self._content = b""
        self.status_code = 404
        self.url = url


def make_request(url, clear_cookies=True, timeout=60, *args, **kwargs):
    if clear_cookies:
        sess.cookies.clear()

    retry_count = 0
    max_retries = 5
    while True:
        try:
            response = sess.get(url, headers=headers, timeout=timeout, *args, **kwargs)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            retry_count += 1
            print("Error with connection ('{msg}'); about to perform retry {count} of {trymax}."
                  .format(msg=str(e), count=retry_count, trymax=max_retries))
            time.sleep(retry_count * 1)
            if retry_count >= max_retries:
                return Dummy404ResponseObject(url=url)

    if response.status_code != 200:
        print("NOT FOUND:", url)

    return response


def make_fully_qualified_url(url):
    base = 'https://blockly-games.appspot.com'
    if url.startswith("../images"):
        return base + url[2:]
    if url.startswith("../scripts"):
        return base + url[2:]
    if url.startswith("//"):
        return "http:" + url
    if url.startswith("/"):
        return base + url
    if not url.startswith("http"):
        return "%s/%s" % (base, url)
    return url


if __name__ == '__main__':
    """
    This code will run when the sushi chef is called from the command line.
    """
    chef = BlocklyGamesChef()
    chef.main()

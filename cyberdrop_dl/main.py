import argparse
import multiprocessing
import platform
from argparse import Namespace
import asyncio
import logging
from asyncio.proactor_events import _ProactorBasePipeTransport
from pathlib import Path
from functools import wraps

from colorama import Fore
from yarl import URL

from . import __version__ as VERSION
from cyberdrop_dl.base_functions.base_functions import clear, log, logger, purge_dir, regex_links
from cyberdrop_dl.base_functions.data_classes import AuthData, SkipData
from cyberdrop_dl.base_functions.sql_helper import SQLHelper
from cyberdrop_dl.client.client import Client
from cyberdrop_dl.client.downloaders import get_downloaders
from cyberdrop_dl.scraper.scraper import scrape


def parse_args():
    parser = argparse.ArgumentParser(description="Bulk downloader for multiple file hosts")
    parser.add_argument("-V", "--version", action="version", version="%(prog)s " + VERSION)
    parser.add_argument("-i", "--input-file", type=Path, help="file containing links to download", default="URLs.txt")
    parser.add_argument("-o", "--output-folder", type=Path, help="folder to download files to", default="Downloads")
    parser.add_argument("--log-file", help="log file to write to", default="downloader.log")
    parser.add_argument("--db-file", help="history database file to write to", default="download_history.sqlite")
    parser.add_argument("--threads", type=int, help="number of threads to use (0 = max)", default=0)
    parser.add_argument("--attempts", type=int, help="number of attempts to download each file", default=10)
    parser.add_argument("--connection-timeout", type=int, help="number of seconds to wait attempting to connect to a URL during the downloading phase", default=15)
    parser.add_argument("--disable-attempt-limit", help="disables the attempt limitation", action="store_true")
    parser.add_argument("--include-id", help="include the ID in the download folder name", action="store_true")
    parser.add_argument("--exclude-videos", help="skip downloading of video files", action="store_true")
    parser.add_argument("--exclude-images", help="skip downloading of image files", action="store_true")
    parser.add_argument("--exclude-audio", help="skip downloading of audio files", action="store_true")
    parser.add_argument("--exclude-other", help="skip downloading of images", action="store_true")
    parser.add_argument("--ignore-history", help="This ignores previous download history", action="store_true")
    parser.add_argument("--output-last-forum-post", help="Separates forum scraping into folders by post number", action="store_true")
    parser.add_argument("--proxy", help="HTTP/HTTPS proxy used for downloading, format [protocal]://[ip]:[port]", default=None)
    parser.add_argument("--separate-posts", help="Separates forum scraping into folders by post number", action="store_true")
    parser.add_argument("--xbunker-username", type=str, help="username to login to xbunker", default=None)
    parser.add_argument("--xbunker-password", type=str, help="password to login to xbunker", default=None)
    parser.add_argument("--socialmediagirls-username", type=str, help="username to login to socialmediagirls", default=None)
    parser.add_argument("--socialmediagirls-password", type=str, help="password to login to socialmediagirls", default=None)
    parser.add_argument("--simpcity-username", type=str, help="username to login to simpcity", default=None)
    parser.add_argument("--simpcity-password", type=str, help="password to login to simpcity", default=None)
    parser.add_argument("--jdownloader-enable", help="enables sending unsupported URLs to a running jdownloader2 instance to download", action="store_true")
    parser.add_argument("--jdownloader-username", type=str, help="username to login to jdownloader", default=None)
    parser.add_argument("--jdownloader-password", type=str, help="password to login to jdownloader", default=None)
    parser.add_argument("--jdownloader-device", type=str, help="device name to login to for jdownloader", default=None)
    parser.add_argument("--skip", dest="skip_hosts", choices=SkipData.supported_hosts, help="This removes host links from downloads", action="append", default=[])
    parser.add_argument("--ratelimit", type=int, help="this will add a ratelimiter to requests made in the program during scraping, the number you provide is in requests/seconds", default=50)
    parser.add_argument("--throttle", type=int, help="This is a throttle between requests during the downloading phase, the number is in seconds", default=0.5)
    parser.add_argument("links", metavar="link", nargs="*", help="link to content to download (passing multiple links is supported)", default=[])
    args = parser.parse_args()
    return args


async def download_all(args: argparse.Namespace):
    await clear()
    await log(f"We are running version {VERSION} of Cyberdrop Downloader", Fore.WHITE)
    print_args = Namespace(**vars(args)).__dict__
    print_args['xbunker_password'] = '!REDACTED!'
    print_args['socialmediagirls_password'] = '!REDACTED!'
    print_args['simpcity_password'] = '!REDACTED!'
    print_args['jdownloader_password'] = '!REDACTED!'

    logging.debug(f"Starting downloader with args: {print_args}")
    input_file = args.input_file
    if not input_file.is_file():
        input_file.touch()
        await log(f"{input_file} created. Populate it and retry.")
        exit(1)

    client = Client(args.ratelimit, args.throttle)
    SQL_helper = SQLHelper(args.ignore_history, args.db_file)
    await SQL_helper.sql_initialize()

    threads = args.threads if args.threads != 0 else multiprocessing.cpu_count()

    links = args.links
    links = list(map(URL, links))

    with open(input_file, "r", encoding="utf8") as f:
        links += await regex_links(f.read())

    links = list(filter(None, links))

    if not links:
        await log("No links found, check the URL.txt\nIf the link works in your web browser, "
                  "please open an issue ticket with me.", Fore.RED)

    output_url_file = None

    if args.output_last_forum_post:
        output_url_file: Path = input_file.parent / "URLs_last_post.txt"
        if output_url_file.exists():
            output_url_file.unlink()
            output_url_file.touch()

    xbunker_auth = AuthData(args.xbunker_username, args.xbunker_password)
    socialmediagirls_auth = AuthData(args.socialmediagirls_username, args.socialmediagirls_password)
    simpcity_auth = AuthData(args.simpcity_username, args.simpcity_password)
    jdownloader_auth = AuthData(args.jdownloader_username, args.jdownloader_password)
    skip_data = SkipData(args.skip_hosts)
    excludes = {'videos': args.exclude_videos, 'images': args.exclude_images, 'audio': args.exclude_audio,
                'other': args.exclude_other}
    content_object = await scrape(links, client, args.include_id, args.jdownloader_enable, args.jdownloader_device, xbunker_auth, socialmediagirls_auth,
                                  simpcity_auth, jdownloader_auth, args.separate_posts, skip_data,
                                  [args.output_last_forum_post, output_url_file])

    if await content_object.is_empty():
        logging.error('ValueError No links')
        await log("No links found duing scraping, check passwords or that the urls are accessible", Fore.RED)
        await log("This program does not currently support password protected albums.", Fore.RED)
        exit(0)
    await clear()

    downloaders = await get_downloaders(content_object, folder=args.output_folder, attempts=args.attempts,
                                        disable_attempt_limit=args.disable_attempt_limit, max_workers=threads,
                                        excludes=excludes, SQL_helper=SQL_helper, client=client, proxy=args.proxy)

    for downloader in downloaders:
        await downloader.download_content(conn_timeout=args.connection_timeout)
    logger.debug("Finished")

    partial_downloads = [str(f) for f in args.output_folder.rglob("*.part") if f.is_file()]

    await log('Purging empty directories')
    await purge_dir(args.output_folder)

    await log('Finished downloading. Enjoy :)')
    if partial_downloads:
        await log('There are still partial downloads in your folders, please re-run the program.')


def silence_event_loop_closed(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except RuntimeError as e:
            if str(e) != 'Event loop is closed':
                raise
    return wrapper


def main(args=None):
    if not args:
        args = parse_args()
    logging.basicConfig(
        filename=args.log_file,
        level=logging.DEBUG,
        format="%(asctime)s:%(levelname)s:%(module)s:%(filename)s:%(lineno)d:%(message)s",
        filemode="w"
    )

    if platform.system() == 'Windows':
        # Silence the "Event loop is closed" exception here.
        _ProactorBasePipeTransport.__del__ = silence_event_loop_closed(_ProactorBasePipeTransport.__del__)

    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(download_all(args))
        loop.run_until_complete(asyncio.sleep(5))
        loop.close()
    except RuntimeError:
        pass


if __name__ == '__main__':
    print("""
    STOP! If you're just trying to download files, check the README.md file for instructions.
    If you're developing this project, use start.py instead.
    """)
    exit()

import math
import requests
from bs4 import BeautifulSoup
import asyncio
from pyppeteer import launch
import argparse
import os.path
import logging
from urllib.parse import urlparse
import multiprocessing
import time
import warnings
from datetime import datetime



bad_status_codes = [301, 303, 404]
bad_texts = ["not found", "not exist", "don't exist", "can't be found", "invalid page", "invalid webpage", "invalid path"]
probable_html_tags = ["h1", "h2", "h3", "title"]

def check_redirects(response):
    logging.info("  [*] Checking if webpage with no redirects returns a bad code")
    origin_url = urlparse(response.url)

    # We create a list of probable simple redirects
    origin_list = [
        origin_url.hostname,
        "http://" + origin_url.hostname,
        "https://" + origin_url.hostname,
        "http://" + origin_url.hostname + "/",
        "https://" + origin_url.hostname + "/",
        "http://" + origin_url.hostname + "/#",
        "https://" + origin_url.hostname + "/#",
        "http://" + origin_url.hostname + ":80",
        "https://" + origin_url.hostname + ":443",
        "http://" + origin_url.hostname + ":80/",
        "https://" + origin_url.hostname + ":443/",
        "http://" + origin_url.hostname + ":80/#",
        "https://" + origin_url.hostname + ":443/#",
    ]

    # Check new and old urls
    if response.history:
        for resp in response.history:
            if resp.is_redirect or resp.is_permanent_redirect:
                if resp.url in origin_list:
                    logging.info("      [-] Bad redirect found: {}".format(response.url))
                    return True
    return False

def requests_page_titles(response):
    global bad_texts
    soup = BeautifulSoup(response.text, 'html.parser')

    logging.info("  [*] Searching for keywords using Beautiful Soup")
    # We check if any of the htlm tags has some text similar to the ones in bad_texts array
    for prob_tag in probable_html_tags:
        for tag in soup.find_all(prob_tag):
            for bad_text in bad_texts:
                if bad_text in tag.get_text().lower():
                    logging.info("      [-] Bad text found: {}".format(bad_text))
                    return True


async def puppeteer_page_titles(url):

    logging.info("  [*] Using Pyppeteer to find bad tags in dynamic html")

    try:
        async with asyncio.TimeoutError(30):  # timeout of 30 seconds
            browser = await launch({"headless": True})
            page = await browser.newPage()
            page.setDefaultNavigationTimeout(15000); #Max timeout reduced to 15s
    except asyncio.TimeoutError:
        logging.error("Browser launch timed out")

    # navigate to the page
    try:
        await page.goto(url)
        # locate the search box
        # We check if any of the htlm tags has some text similar to the ones in bad_texts array
        #await page.waitForSelector("title")
        html = await page.content()
        await browser.close()
        parsed_url = urlparse(page.url)
    except:
        logging.info("      [!] Timeout while awaiting for tags or connecting. Page may be down.")
        await browser.close()
        return False

    # Redirection case
    if url != page.url:
        logging.info("      [!] Redirection detected to {}".format(page.url))
        if parsed_url.path in ["/", "/#"]:
            logging.info("      [-] Redirected to root!")
            return True

    soup = BeautifulSoup(html, 'html.parser')
    for prob_tag in probable_html_tags:
        for tag in soup.find_all(prob_tag):
            for bad_text in bad_texts:
                if bad_text in tag.get_text().lower():
                    logging.info("      [-] Bad text found: {}".format(bad_text))
                    return True

    return False


async def check_all_methods(lines, good_urls):

    # This checks for 404, so by default the URL is added and only removed if this is a fake 404
    past_response = ""
    for url in lines:
        logging.info("[*] Checking URL: {}".format(url))
        good_urls.append(url + "\n")
        
        try:
            r = requests.get(url, timeout=15) #Max timeout reduced to 15s
        except:
            logging.info("  [!] Timeout while awaiting for get request. Page may be down.")
            continue
        
        if past_response == r.text:
            logging.info("  [!] Same page detected. Skipping.")
            past_response = r.text
            good_urls.remove(url + "\n")
            continue

        past_response = r.text

        # Splitted in three ifs to improve timing: If redirect
        if check_redirects(r):
            good_urls.remove(url + "\n")

        if requests_page_titles(r):
            good_urls.remove(url + "\n")

        ppt = await puppeteer_page_titles(url)
        if ppt:
            good_urls.remove(url + "\n")

        logging.info("[*] Url found legit!")


def multiprocess_executer(args):
    manager = multiprocessing.Manager()
    good_urls = manager.list() # Creates a special type of list that can be safely manipulated by multiple processes.
    num_processes = args.processes
    jobs = []

    if os.path.isfile(args.input_file):
        with open(args.input_file, "r") as ifile:
            lines = ifile.read().splitlines()

            # Here we are splitting the file in multiple pieces for better processing
            parts_len = math.ceil(len(lines)/num_processes)
            parts = list(chunks_from_lines(lines, parts_len))
    else:
        logging.error("[!] File not found! {}".format(args.input_file))
        parser.print_help()

    for i in range(num_processes):
        p = multiprocessing.Process(target=worker, args=(parts[i], good_urls))
        jobs.append((p,parts[i]))
        p.start()

    # Give a max time running of 12 hours
    start_time = datetime.now()

    while (datetime.now() - start_time).seconds < 12*60*60:  # run for 12 hours max
        time.sleep(1)  # short sleep to prevent busy looping
        for proc, parts in jobs:
            if not proc.is_alive():
                jobs.remove((proc, parts))  # remove completed processes from the list

        if not jobs:  # if no jobs remain, break out of the loop
            break

    else:  # if the loop completed normally (not by 'break')
        for proc, parts in jobs:  # terminate all remaining jobs
            if proc.is_alive():
                for part in parts:
                    if not part in good_urls:
                        good_urls.append(part + "\n")
                print("Process is still running, timeout occurred.")
                proc.terminate()

    with open(args.output_file, "w") as ofile:
        ofile.writelines(good_urls)

# Aux function to divide a file into chunks
def chunks_from_lines(l, n):
    # looping till length l
    for i in range(0, len(l), n):
        yield l[i:i + n]


def worker(lines, good_urls):
    asyncio.get_event_loop().run_until_complete(check_all_methods(lines, good_urls))


# Press the green button in the gutter to run the script.
if __name__ == '__main__':

    warnings.filterwarnings("ignore", category=UserWarning, module='bs4', message='.*looks like a filename.*')

    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input_file", help="Input file with urls on it (one per line)", type=str, required=True)
    parser.add_argument("-o", "--output_file", help="Output file with good urls (one per line)", type=str, required=True)
    parser.add_argument('-v', '--verbose', help="Be verbose", action="store_const", dest="loglevel", const=logging.INFO)
    parser.add_argument('-p', '--processes', help="Number of processes (default number of cpus)", type=int, default=multiprocessing.cpu_count())
    args = parser.parse_args()

    if not os.path.isfile(args.input_file):
        logging.error("File not found! {}".format(args.input_file))
        exit()

    logging.basicConfig(level=args.loglevel)
    try:
        os.remove(os.path.realpath(args.output_file))
    except:
        pass

    multiprocess_start = time.time()
    multiprocess_executer(args)
    multiprocess_end = time.time()

    print("Multiprocess time: {}".format(multiprocess_end - multiprocess_start))
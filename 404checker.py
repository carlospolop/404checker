import math
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import argparse
import os.path
import logging
from urllib.parse import urlparse
import multiprocessing
import time
import warnings
from datetime import datetime
import concurrent.futures
import threading
from urllib3.exceptions import InsecureRequestWarning
import urllib3


urllib3.disable_warnings(InsecureRequestWarning)
BAD_TEXTS = ["not found", "not exist", "don't exist", "can't be found", "invalid page", "invalid webpage", "invalid path", "cannot get path "]
PROBABLE_HTML_TAGS = ["h1", "h2", "h3", "title"]
CACHE_404 = {}
LOCK_GOOD_URLS = threading.Lock()
LOCK_JS_URLS = threading.Lock()


def check_redirects(url, response, response_404):
    #logging.info("  [*] Checking if webpage with no redirects returns a bad code")
    origin_url = urlparse(url)

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

    # If redirects URls in list, bad
    if response.history:
        if response.url in origin_list:
            logging.info("      [-] Bad redirect found: {}".format(response.url))
            return True
        for resp in response.history:
            if resp.is_redirect or resp.is_permanent_redirect:
                if resp.headers.get("Location", "none") in origin_list:
                    logging.info("      [-] Bad redirect found from {} to {}".format(response.url, resp.headers.get("Location", "none")))
                    return True
    
    # If same URL as a real 404, bad
    if response_404:
        if response.url == response_404.url:
            logging.info("      [-] Bad redirect with 404 found: {}".format(response.url))
            return True

    return False

def requests_page_titles(response):
    global BAD_TEXTS, PROBABLE_HTML_TAGS
    soup = BeautifulSoup(response.text, 'html.parser')

    #logging.info("  [*] Searching for keywords using Beautiful Soup")
    # We check if any of the htlm tags has some text similar to the ones in BAD_TEXTS array
    for prob_tag in PROBABLE_HTML_TAGS:
        for tag in soup.find_all(prob_tag):
            for bad_text in BAD_TEXTS:
                if bad_text in tag.get_text().lower():
                    logging.info("      [-] Bad text found: {}".format(bad_text))
                    return True


def js_checks(ini_url, page):
    global BAD_TEXTS, PROBABLE_HTML_TAGS
    # Having accessed the URL with a browser, check the response

    html = page.content()
    parsed_url = urlparse(page.url)
    parsed_ini_url = urlparse(ini_url)

    # Redirection case
    if ini_url != page.url:
        logging.info("      [!] JS redirection detected to {}".format(page.url))
        if parsed_url.path in ["/", "/#"] and parsed_ini_url.path != parsed_url.path:
            logging.info(f"      [-] JS of {ini_url} redirected to root!")
            return True

    soup = BeautifulSoup(html, 'html.parser')
    for prob_tag in PROBABLE_HTML_TAGS:
        for tag in soup.find_all(prob_tag):
            for bad_text in BAD_TEXTS:
                if bad_text in tag.get_text().lower():
                    logging.info("      [-] JS bad text found in {url}: {}".format(bad_text))
                    return True

    return False


def check_non_js_methods(url, good_urls, user_agent, check_js_urls_list):
    global CACHE_404, LOCK_GOOD_URLS, LOCK_JS_URLS

    headers = {
        'User-Agent': user_agent
    }

    logging.info("[*] Checking URL: {}".format(url))

    try:
        r = requests.get(url, timeout=5, verify=False, headers=headers)
    except:
        logging.info("  [!] Timeout while awaiting for get request. Retrying..")
        try:
            r = requests.get(url, timeout=10, verify=False, headers=headers) #Max timeout reduced to 10s
        except:
            logging.info(f"  [!] Timeout while awaiting for get request for {url}. Page might be down. Removing")
            return
    
    # If status code is 404, it's 404
    if str(r.status_code) == "404":
        return
    
    # If "not found" texts in titles of HTML, it's 404
    if requests_page_titles(r):
        return
    
    # Get a real 404 in the same folder
    if len(url.split("/")) > 3:
        url_404 = "/".join(url.split("/")[:-1])+"/real404i32rohuf"
    else: # In case something like "https://example.com" withuot not extra path
        url_404 = url + "/real404i32rohuf"

    if url_404 in CACHE_404:
        r_404 = CACHE_404[url_404]
    
    else:
        r_404 = None
        try:
            r_404 = requests.get(url_404, timeout=5, verify=False, headers=headers, allow_redirects=True)
        except Exception as e:
            logging.info(f"  [!] Timeout while awaiting for 404 get request. Retrying... \n{e}")
            try:
                r_404 = requests.get(url_404, timeout=10, verify=False, headers=headers, allow_redirects=True) #Max timeout reduced to 10s
            except Exception as e:
                logging.info(f"  [!] Timeout while awaiting for 404 get request. Page might be down. Removing\n {e}")
                return
    
        if r_404:
            CACHE_404[url_404] = r_404
    
    # If redirects to root or suspicious valid page (like the one for the real 404), it's 404
    if check_redirects(url, r, r_404):
        return
    
    # Check if other status codes are used as 404
    if r_404 != None:
        if r_404.status_code == r.status_code and str(r.status_code).startswith("4") or str(r.status_code).startswith("5"):
            logging.info(f"  [!] Weird 404 status code detected: {r.status_code} for {url} Skipping.")
            return
        
        # If same content as real 404, it's a 404
        if r_404.text == r.text:
            logging.info(f"  [!] Same content as error detected for {url}. Skipping.")
            return

        # If different status codes from real 404, then it might not be a 404 and no need to check with JS engine
        if r_404.status_code != r.status_code:
            with LOCK_GOOD_URLS: good_urls.append(r.url)
            logging.info(f"[*] {url} found legit in {r.url}")
            return
    else:
        print(f"No 404: {url_404}")
    
    if any(enable_js_txt in r.text.lower() for enable_js_txt in ["enable javascript", "requires javascript", "javascript is disabled"]):
        # Use a JS engine to check if 404
        with LOCK_JS_URLS: check_js_urls_list.append(r.url) # Check the final url after redirects (as it might end up being duplicated)
    else:
        with LOCK_GOOD_URLS: good_urls.append(r.url) # Add the final url after redirects if found legit
        logging.info(f"[*] {url} found legit in {r.url} as no JS required!")


def multithread_executor(args, good_urls, check_js_urls_list):
    num_threads = args.threads
    user_agent = args.user_agent

    if os.path.isfile(args.input_file):
        with open(args.input_file, "r") as ifile:
            urls = ifile.read().splitlines()
    else:
        logging.error("[!] File not found! {}".format(args.input_file))
        parser.print_help()

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(check_non_js_methods, url, good_urls, user_agent, check_js_urls_list) for url in urls]

        # Wait for all futures and check for exceptions
        for future in futures:
            try:
                future.result()  # This will re-raise any exception caught during the execution
            except Exception as e:
                print(f"Thread exception: {e}")



def check_js_methods(urls, p_good_urls, user_agent):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_default_timeout(15000); #Max timeout reduced to 15s
    
            # navigate to the page
            for url in urls:
                try:
                    logging.info(f"  [*] Checking dynamically: {url}")
                    page.goto(url)
                    bad_js_title = js_checks(url, page)
                    if bad_js_title:
                        continue
                
                    p_good_urls.append(page.url) # Store the final URL so if difefrent pages redirect to the same one, duplicates are removed
                except:
                    logging.info(f"      [!] Timeout while awaiting for tags or connecting. {url} may be down.")
        
            browser.close()
    except Exception as e:
        logging.error(f"Browser launch timed out: {e}")

    
def multiprocess_executor(args, good_urls, check_js_urls_list):
    manager = multiprocessing.Manager()
    p_good_urls = manager.list() # Creates a special type of list that can be safely manipulated by multiple processes.
    num_processes = args.processes
    user_agent = args.user_agent
    jobs = []

    if check_js_urls_list:
        parts_len = math.ceil(len(check_js_urls_list)/num_processes)
        parts = list(chunks_from_lines(check_js_urls_list, parts_len))

        for i in range(num_processes):
            if i < len(parts):
                p = multiprocessing.Process(target=check_js_methods, args=(parts[i], p_good_urls, user_agent))
                jobs.append((p,parts[i]))
                p.start()

        # Give a max time running of 10 hours
        start_time = datetime.now()

        while (datetime.now() - start_time).seconds < 10*60*60:  # run for 10 hours max
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
                            p_good_urls.append(part)
                    print("Process is still running, timeout occurred.")
                    proc.terminate()
    else:
        print("No JS URLs to check")

    all_good_urls = list(set(p_good_urls + good_urls))
    with open(args.output_file, "w") as ofile:
        for url in all_good_urls:
            ofile.write(f"{url}\n")


# Aux function to divide a file into chunks
def chunks_from_lines(l, n):
    # looping till length l
    for i in range(0, len(l), n):
        yield l[i:i + n]


# Press the green button in the gutter to run the script.
if __name__ == '__main__':

    warnings.filterwarnings("ignore", category=UserWarning, module='bs4', message='.*looks like a filename.*')

    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input_file", help="Input file with urls on it (one per line)", type=str, required=True)
    parser.add_argument("-o", "--output_file", help="Output file with good urls (one per line)", type=str, required=True)
    parser.add_argument('-v', '--verbose', help="Be verbose", action="store_const", dest="loglevel", const=logging.INFO)
    parser.add_argument('-t', '--threads', help="Number of threads (default 50)", type=int, default=50)
    parser.add_argument('-p', '--processes', help="Number of browser processes (default number of cpus)", type=int, default=int(multiprocessing.cpu_count()) if multiprocessing.cpu_count() > 1 else 1)
    parser.add_argument('-u', '--user-agent', help="User Agent", type=str, default="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    args = parser.parse_args()

    if not os.path.isfile(args.input_file):
        logging.error("File not found! {}".format(args.input_file))
        exit()

    logging.basicConfig(level=args.loglevel)
    try:
        os.remove(os.path.realpath(args.output_file))
    except:
        pass

    good_urls, check_js_urls_list = [], []

    multithread_start = time.time()
    multithread_executor(args, good_urls, check_js_urls_list)
    multithread_start = time.time()
    print("Multithread time: {}".format(multithread_start - multithread_start))

    check_js_urls_list = list(set(check_js_urls_list))
    multiprocess_start = time.time()
    multiprocess_executor(args, good_urls, check_js_urls_list)
    multiprocess_end = time.time()
    print("Multiprocess time: {}".format(multiprocess_end - multiprocess_start))

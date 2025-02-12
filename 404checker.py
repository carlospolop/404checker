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
import random
import tldextract
import xml.etree.ElementTree as ET
import re


urllib3.disable_warnings(InsecureRequestWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
BAD_TEXTS = ["not found", "not exist", "don't exist", "can't be found", "invalid page", "invalid webpage", "invalid path", "cannot get path "]
PROBABLE_HTML_TAGS = ["h1", "h2", "h3", "title"]
CACHE_404 = {}
LOCK_GOOD_URLS = threading.Lock()
LOCK_JS_URLS = threading.Lock()




#############################
#### REMOVE USELESS URLS ####
#############################

def get_path_parts(url):
    """
    **Splits** the URL path into **folders** (ignoring empty segments).
    """
    parsed = urlparse(url)
    # Example: 'http://example.com/en/articles/page' -> ['en', 'articles', 'page']
    path_parts = [p for p in parsed.path.split('/') if p]
    return parsed, path_parts


def remove_urls_with_large_depth(urls, max_depth=20):
    """
    **Removes** URLs that have a path depth larger than `max_depth`.
    """
    filtered = []
    for url in urls:
        _, path_parts = get_path_parts(url)
        # **Check if depth is > max_depth**
        if len(path_parts) <= max_depth:
            filtered.append(url)
    return filtered


def remove_urls_with_repeated_folders(urls, max_repeats=2):
    """
    **Removes** URLs that have the same folder name repeated
    more than `max_repeats` times **in a row**.
    """
    filtered = []
    for url in urls:
        _, path_parts = get_path_parts(url)
        # **Check for repeated folder names in a row**
        has_too_many_repeats = False
        current_count = 1
        for i in range(1, len(path_parts)):
            if path_parts[i] == path_parts[i-1]:
                current_count += 1
                if current_count > max_repeats:
                    has_too_many_repeats = True
                    break
            else:
                current_count = 1
        if not has_too_many_repeats:
            filtered.append(url)
    return filtered

def normalize_languages(urls):
    """
    **Normalization** steps based on **grouping** URLs that differ only by their **first folder**:

    1. Group URLs by (scheme, domain, rest_of_path_ignoring_first_folder).
    2. If a group has only 1 URL, keep it.
    3. If multiple:
       - Check if any first folder is "English-like" (starts with `en`).
         If so, pick it as default.
         Else if any is `zh`, pick that.
         Else if any is `es`, pick that.
         Else pick the first folder in that group.
       - Build a **single** URL using the chosen folder and the shared rest path.
    4. **Return** all final URLs (duplicates removed).
    """

    # A helper to detect an "English-like" folder (like "en", "en-us", etc.)
    def is_english_folder(folder):
        # Lowercase check: does it start with "en"?
        return folder == "en" or (folder.lower().startswith("en-") and len(folder) < 7)

    grouped = {}  # (scheme, domain, rest_path) -> list of dicts with {folder, original_url}

    for url in urls:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        domain = parsed.netloc.lower()

        # Split path into folders
        path_parts = [p for p in parsed.path.split('/') if p]

        if not path_parts:
            # No folders => no "first folder", rest path is empty
            first_folder = ""
            rest_path = ""
        else:
            # We'll ALWAYS treat the first part as "folder" (whether language or not)
            first_folder = path_parts[0]
            rest_path = "/".join(path_parts[1:])

        key = (scheme, domain, rest_path)

        if key not in grouped:
            grouped[key] = []
        grouped[key].append({
            "folder": first_folder,
            "original_url": url
        })

    final_urls = set()

    # Now let's pick the final form for each group
    for (scheme, domain, rest_path), items in grouped.items():
        # If there's only 1 item in this group, we keep it as is
        if len(items) == 1:
            final_urls.add(items[0]["original_url"])
            continue

        # If there's more than 1 item, we pick a default folder
        chosen_folder = None

        # 1) If there's an "English-like" folder
        english_candidates = [it["folder"] for it in items if is_english_folder(it["folder"])]
        if english_candidates:
            chosen_folder = english_candidates[0]
        else:
            # 2) If there's a "zh"
            zh_candidates = [it["folder"] for it in items if it["folder"].lower() == "zh"]
            if zh_candidates:
                chosen_folder = zh_candidates[0]
            else:
                # 3) If there's an "es"
                es_candidates = [it["folder"] for it in items if it["folder"].lower() == "es"]
                if es_candidates:
                    chosen_folder = es_candidates[0]
                else:
                    # 4) Otherwise pick the first folder from the group
                    chosen_folder = items[0]["folder"]

        # Construct a single final URL for the group
        # If chosen_folder is empty, that means "root" (no folder).
        # We'll build the path accordingly
        # e.g. scheme://domain[/chosen_folder][/rest_path]
        path_str = ""
        if chosen_folder:
            path_str += "/" + chosen_folder
        if rest_path:
            path_str += "/" + rest_path

        final_url = f"{scheme}://{domain}{path_str}"
        final_urls.add(final_url)

    return list(final_urls)

def filter_urls_by_numeric_and_folder_limits(all_urls):
    """
    1) Group URLs by (scheme, netloc, 'all but last path folder(s)').
    2) In each group:
       - Keep only first 20 URLs whose final path segment is purely numeric.
       - Then from the entire group (numeric + non-numeric), keep only the first 50.
    3) Return the filtered list in the original order.
    """

    # We'll group by (scheme, netloc, 'folder_path_up_to_last_segment')
    # For example: http://example.com/foo/bar/123
    #   - final_segment = "123"
    #   - group_key = (scheme="http", netloc="example.com", folder_path="/foo/bar")
    #
    # Then store info about whether final_segment is numeric, plus original index to preserve order.

    # A helper to detect if a final segment is purely numeric
    def is_numeric_segment(segment):
        return bool(re.fullmatch(r"\d+", segment))

    grouped = {}
    # We also keep the order in which URLs appear globally:
    # We'll store each URL with its group key, final_segment, a boolean is_numeric, and the original index.
    for idx, url in enumerate(all_urls):
        parsed = urlparse(url)
        scheme = parsed.scheme
        netloc = parsed.netloc

        # Split path into segments
        parts = [p for p in parsed.path.split('/') if p]

        if not parts:
            # No final segment
            final_segment = ""
            folder_path = ""
        else:
            final_segment = parts[-1]
            folder_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""

        group_key = (scheme, netloc, folder_path)

        if group_key not in grouped:
            grouped[group_key] = []

        grouped[group_key].append({
            "url": url,
            "is_numeric": is_numeric_segment(final_segment),
            "index": idx
        })

    # Now let's apply the per-group rules
    final_urls = []

    for group_key, items in grouped.items():
        # We want to preserve original order, so sort 'items' by their "index"
        items.sort(key=lambda x: x["index"])

        # 1) Keep only the first 20 with is_numeric == True
        numeric_count = 0
        for item in items:
            if item["is_numeric"]:
                numeric_count += 1
                if numeric_count > 20:
                    # Mark these for removal
                    item["remove"] = True
                else:
                    item["remove"] = False
            else:
                item["remove"] = False

        # 2) Now keep the first 50 overall in this group
        #    We'll do a second pass counting how many we've kept so far
        kept_in_group = 0
        for item in items:
            if not item["remove"]:
                kept_in_group += 1
                if kept_in_group > 50:
                    # Mark for removal if we're over 50
                    item["remove"] = True

        # Collect the final set from this group
        for item in items:
            if not item["remove"]:
                final_urls.append(item)

    # Finally, re-sort by original index to restore global order
    final_urls.sort(key=lambda x: x["index"])

    # Extract just the URLs
    return [x["url"] for x in final_urls]


def filter_and_normalize_urls(all_urls):
    """
    Combined pipeline:
      1) **Remove** URLs with depth > 20.
      2) **Remove** URLs with repeated folders.
      3) **Normalize** language paths (favor root, else 'en', 'xh', 'es', else first).
    """
    all_urls = remove_urls_with_large_depth(all_urls) # If too many folders, remove
    all_urls = remove_urls_with_repeated_folders(all_urls) # If 3 or more repeated folders with the same name, remove
    all_urls = normalize_languages(all_urls) # If same but in different languages, keep only one
    all_urls = filter_urls_by_numeric_and_folder_limits(all_urls) # If too many files inside a folder, reduce
    return all_urls




######################################
#### CHECK URLS BASED ON SITEMAPS ####
######################################

# We will store data in this global dictionary to avoid re-checking the same domain
# Structure:
# domain_data = {
#   "example.com": {
#       "subdomains": {
#           "www": {
#               "sitemaps": set(["https://www.example.com/sitemap_index.xml", ...]),
#               "discovered_urls": set(["https://www.example.com/page1", ...])
#           },
#           "": { ... },  # empty means "root" domain
#           ...
#       },
#       "all_discovered_urls": set([...])  # union of discovered_urls from all subdomains
#   },
#   ...
# }
domain_data = {}
sitemaps_downloaded = set()

def get_tld_and_subdomain(url):
    """
    **Parses** the given URL to extract the **TLD** (e.g. 'example.com')
    and the **subdomain** (e.g. 'blog' in 'blog.example.com').
    Returns (tld, subdomain).
    """
    ext = tldextract.extract(url)
    tld = f"{ext.domain}.{ext.suffix}"  # e.g. "example.com"
    subdomain = ext.subdomain or ""      # e.g. "blog" or "" if none
    return tld.lower(), subdomain.lower()

def get_robots_url(tld, subdomain=""):
    """
    Returns a **robots.txt** URL for the **tld** and optional **subdomain**.
    For a subdomain 'blog' and tld 'example.com', 
    it might be 'https://blog.example.com/robots.txt'.
    """
    if subdomain:
        return f"https://{subdomain}.{tld}/robots.txt"
    else:
        return f"https://{tld}/robots.txt"

def get_root_sitemap_url(tld, subdomain=""):
    """
    Returns the **root** sitemap.xml path for a given TLD + subdomain.
    """
    if subdomain:
        return f"https://{subdomain}.{tld}/sitemap.xml"
    else:
        return f"https://{tld}/sitemap.xml"

def fetch_robots_sitemaps(tld, subdomain):
    """
    **Fetch** the robots.txt for (tld, subdomain) and **parse** out any 'Sitemap:' lines.
    Returns a set of discovered sitemap URLs.
    """
    sitemaps_found = set()
    url = get_robots_url(tld, subdomain)
    
    try:
        print("Fetching robots.txt:", url)
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                # Lines can look like: "Sitemap: https://example.com/sitemap_index.xml"
                if line.lower().startswith("sitemap:"):
                    # Extract the URL after "Sitemap:"
                    #print("Discovered sitemap:", line)
                    sitemap_url = line.split(":", 1)[1].strip()
                    sitemaps_found.add(sitemap_url)
    except requests.RequestException:
        # Could not fetch robots.txt
        pass
    
    return sitemaps_found

def parse_sitemap(sitemap_url, discovered_urls, discovered_sitemaps):
    """
    **Parses** the given sitemap URL (which may be an **index** of multiple sitemaps 
    or a **regular** sitemap of URLs).

    - If it's a sitemap **index**, we grab each child **<loc>** as a new sitemap to parse.
    - If it's a **regular** sitemap, we grab each **<url><loc>** entry as a discovered URL.

    Updates `discovered_urls` (set of URLs) 
    and `discovered_sitemaps` (set of sitemaps) in-place.
    """
    global sitemaps_downloaded
    
    try:
        #print(f"Checking sitemap {sitemap_url}")
        if sitemap_url in sitemaps_downloaded:
            return # Already downloaded
        
        sitemaps_downloaded.add(sitemap_url)
        resp = requests.get(sitemap_url, timeout=5)
        if resp.status_code != 200:
            return  # Not found or error

        # Parse the XML
        root = ET.fromstring(resp.content)

        # The root tag can be {...}sitemapindex or {...}urlset
        tag_lower = root.tag.lower()
        if "sitemapindex" in tag_lower:
            # This is an index of sitemaps
            for child in root.findall(".//{*}sitemap"):
                loc_el = child.find("{*}loc")
                if loc_el is not None and loc_el.text:
                    new_sitemap = loc_el.text.strip()
                    if new_sitemap not in discovered_sitemaps:
                        #print(f"Discovered sitemap {new_sitemap} from {sitemap_url}")
                        discovered_sitemaps.add(new_sitemap)
                        # parse recursively
                        parse_sitemap(new_sitemap, discovered_urls, discovered_sitemaps)
        elif "urlset" in tag_lower:
            # This is a list of URLs
            for child in root.findall(".//{*}url"):
                loc_el = child.find("{*}loc")
                if loc_el is not None and loc_el.text:
                    discovered_urls.add(loc_el.text.strip())
        else:
            # Some sitemaps might have unusual tags, or be empty
            pass

    except requests.RequestException:
        # Network or parse error - skip
        pass
    except ET.ParseError:
        # Not valid XML
        pass

def discover_all_sitemaps_and_urls(tld, subdomain):
    """
    **Discover** all sitemaps and URLs for the given TLD + subdomain:
      1) Fetch & parse robots.txt for its Sitemaps
      2) Check default /sitemap.xml
      3) Recursively parse any discovered sitemaps for more sitemaps
         or actual URLs.
    Stores results in `domain_data[tld]['subdomains'][subdomain]`.
    Also updates `domain_data[tld]['all_discovered_urls']`.
    """
    # Ensure structure is present
    if tld not in domain_data:
        domain_data[tld] = {
            "subdomains": {},
            "all_discovered_urls": set()
        }
    if subdomain not in domain_data[tld]["subdomains"]:
        domain_data[tld]["subdomains"][subdomain] = {
            "sitemaps": set(),
            "discovered_urls": set()
        }

    subdomain_dict = domain_data[tld]["subdomains"][subdomain]

    # 1) Fetch robots
    found_in_robots = fetch_robots_sitemaps(tld, subdomain)
    subdomain_dict["sitemaps"].update(found_in_robots)

    # 2) Try default /sitemap.xml
    sitemap_url = get_root_sitemap_url(tld, subdomain)
    subdomain_dict["sitemaps"].add(sitemap_url)

    # 3) Recursively parse each discovered sitemap
    #    collecting all discovered URLs into subdomain_dict["discovered_urls"]
    #    and new sitemaps into subdomain_dict["sitemaps"]
    sitemaps_to_check = list(subdomain_dict["sitemaps"])

    for sm in sitemaps_to_check:
        parse_sitemap(sm, subdomain_dict["discovered_urls"], subdomain_dict["sitemaps"])

    # 4) Update the TLD's 'all_discovered_urls' with what we found
    domain_data[tld]["all_discovered_urls"].update(subdomain_dict["discovered_urls"])

def check_url_in_sitemaps(url):
    """
    **Check** if the given URL is in the **discovered URLs** for its TLD (+ subdomain).
    Returns **True** if found, **False** if not.
    """
    tld, subdom = get_tld_and_subdomain(url)
    # If we haven't discovered tld yet, obviously we haven't found the URL
    if tld not in domain_data:
        return False

    # We might not always store subdom if it was never discovered, but we can also check
    # the TLD's all_discovered_urls:
    return (url in domain_data[tld]["all_discovered_urls"])

def check_based_on_sitemaps(all_urls, good_urls):
    """
    Main function to loop all input URLs and:
      - Parse TLD + subdomain
      - If TLD is new, discover sitemaps for TLD root + subdomain
      - If TLD exists but subdomain is new, discover sitemaps for subdomain
      - Then check if the URL is known => Return True or False
    """
    unknown_urls = []
    for url in all_urls:
        tld, subdom = get_tld_and_subdomain(url)

        # If TLD not in domain_data, it's new => discover it
        if tld not in domain_data:
            discover_all_sitemaps_and_urls(tld, subdom)

        # If subdomain not in domain_data[tld]["subdomains"], discover that too
        elif subdom not in domain_data[tld]["subdomains"]:
            discover_all_sitemaps_and_urls(tld, subdom)

        # Now check if URL is in the known set
        if check_url_in_sitemaps(url):
            good_urls.append(url)
        else:
            unknown_urls.append(url)

    return unknown_urls





########################
#### check for 404s ####
########################


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

def check_page_titles(response):
    global BAD_TEXTS, PROBABLE_HTML_TAGS
    soup = BeautifulSoup(response.text, 'html.parser')

    #logging.info("  [*] Searching for keywords using Beautiful Soup")
    # We check if any of the htlm tags has some text similar to the ones in BAD_TEXTS array
    for prob_tag in PROBABLE_HTML_TAGS:
        for tag in soup.find_all(prob_tag):
            for bad_text in BAD_TEXTS:
                if bad_text in tag.get_text().lower():
                    logging.info("      [-] Bad text found for url {}: {}".format(response.url, bad_text))
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
    
    # If "not found" texts in titles of HTML, it's 404
    r_404_badpt = None
    if r_404:
        r_404_badpt = check_page_titles(r_404)
    
    r_badpt = check_page_titles(r)
    
    # Try to avoid false positives of the tags checking that the tags are also in the 404 response.
    if r_badpt:
        if r_404_badpt == None or r_404_badpt:
            return
    
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


def multithread_executor(args, all_urls, good_urls, check_js_urls_list):
    num_threads = args.threads
    user_agent = args.user_agent

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(check_non_js_methods, url, good_urls, user_agent, check_js_urls_list) for url in all_urls]

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
    parser.add_argument('-m', '--max-urls', default=50000, help="Max number of URLs (if more the rest will pass)", type=int)
    args = parser.parse_args()

    if not os.path.isfile(args.input_file):
        logging.error("File not found! {}".format(args.input_file))
        exit()

    logging.basicConfig(level=args.loglevel)
    try:
        os.remove(os.path.realpath(args.output_file))
    except:
        pass
    
    all_urls = []
    if os.path.isfile(args.input_file):
        with open(args.input_file, "r") as ifile:
            all_urls = ifile.read().splitlines()
    else:
        logging.error("[!] File not found! {}".format(args.input_file))
        parser.print_help()
        exit(1)

    good_urls, check_js_urls_list = [], []
    
    # Filter and normalize URLs to reduce the number of tests
    print("Started with {} URLs".format(len(all_urls)))
    all_urls = filter_and_normalize_urls(all_urls)
    print("Reduced URLs to {} after filtering".format(len(all_urls)))

    # Check the sitemaps first
    all_urls = check_based_on_sitemaps(all_urls, good_urls)
    print("Reduced URLs to {} after sitemaps".format(len(all_urls)))

    random.shuffle(all_urls)

    if len(all_urls) > args.max_urls:
        print(f"Too many URLs ({len(all_urls)}). Only the first {args.max_urls} will be checked.")
        all_urls = all_urls[:args.max_urls]
        good_urls = good_urls[:args.max_urls]

    multithread_start = time.time()
    multithread_executor(args, all_urls, good_urls, check_js_urls_list)
    multithread_start = time.time()
    print("Multithread time: {}".format(multithread_start - multithread_start))

    check_js_urls_list = list(set(check_js_urls_list))
    multiprocess_start = time.time()
    multiprocess_executor(args, good_urls, check_js_urls_list)
    multiprocess_end = time.time()
    print("Multiprocess time: {}".format(multiprocess_end - multiprocess_start))

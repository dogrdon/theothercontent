#!/usr/bin/env python
# -*- coding: utf-8 -*-

# standard libs
import csv
import json
import sys, os
import time
from urllib.parse import urlparse, urljoin, parse_qs
import requests
import hashlib
import datetime
import logging
from signal import signal, SIGALRM, alarm # for timeout on phantomjs
from functools import wraps # ''
import errno                # ''
import argparse

# selenium for rendering
from selenium import webdriver
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

# beautiful soup for parsing
from bs4 import BeautifulSoup

# multiprocessing for threads
from multiprocessing import Pool

# what else but mongo for safe keeping
import connection as c



'''Handling timeouts'''

class TimeoutError(Exception):
    pass

def timeout(wait_time=30, error_message=os.strerror(errno.ETIME)):
    def decorator(f):
        def _make_timeout(signum, frame):
            print("You timed out: {}".format(error_message))

        def wrapper(*args, **kwargs):
            signal(SIGALRM, _make_timeout)
            alarm(wait_time)
            try:
                result = f(*args, **kwargs)
            finally: 
                alarm(0)
            return result
        return wraps(f)(wrapper)
    return decorator


def fetchSiteGuide(PATHTOSITEGUIDE):
    """Function which takes in the csv of sites and
       where their other content are and produces a 
       more traversable pyton dictionary for 
       extracting article details
    """
    fetched_sites = []
    with open(PATHTOSITEGUIDE, 'r') as f:
        rows = csv.DictReader(f)
        fields = rows.fieldnames
        for row in rows:
            entry = {f: row[f] for f in fields}
            fetched_sites.append(entry)

    return fetched_sites


def checkArticleURL(site, link):
    if link.startswith(site):
        return link
    else:
        return urljoin(site,link)

#@timeout(60)
def getArticles(target):
    print("Getting Articles")
    ARTICLES_MAX = 3

    articles = {}
    site = target['site']
    host = urlparse(site).netloc
    articleDriver = SessionManager(host=host)
    articleDriver.driver.get(target['site'])
    soup = articleDriver.requestParsed()
    articles[site] = [checkArticleURL(site,i.attrs['href']) for i in soup.select(
        target['articles_selector'])[0:ARTICLES_MAX]]
    del articleDriver
    return articles

def _defineSel(selector):
    if '!' in selector:
        return [s.strip() for s in selector.split('!') if s != '']
    else:
        return selector


def _getFullURL(url):
    if url.startswith('//'):
        return 'http:{}'.format(url)
    else:
        return url


def _getFinalURL(url):
    try:
        res = requests.get(url)
        return res.url
    except Exception as e:
        print("Something went wrong getting the final URL for {}: {}".format(url, e))
        return url

def _getImgFormat(url, header):
    possible_formats = ['jpg', 'gif', 'png', 'jpeg']
    if header != '':
        if 'jpeg' in header:
            return '.jpg'
        elif 'png' in header:
            return '.png'
        elif 'gif' in header:
            return '.gif'
        else:
            print('content-type {} not recognized'.format(header))
    else:
        ext = url.split('.')[-1]
        if ext in possible_formats:
            return '.{}'.format(ext)
        else:
            print('could not find an extension for {}, have a look, for now leaving it without one.'.format(url))
            return ''        

#@timeout(200)
def getArticleData(articles_pkg):
    print("Getting Article Content")

    contents = articles_pkg['contents_selector']
    articles = articles_pkg['articles']

    hlSel = _defineSel(articles_pkg['content_hl'])
    imgSel = _defineSel(articles_pkg['content_img'])
    linkSel = _defineSel(articles_pkg['content_link'])
    provider = articles_pkg['farm']
    source = articles_pkg['site']
    article_host = '{}_article'.format(urlparse(source).netloc)

    output = []
    contentDriver = SessionManager(host=article_host)

    for article in articles:
        try:
            contentDriver.driver.get(article)
        except Exception as e:
            print("Problem getting: {} - {}. Moving on".format(article, e))
            continue
        
        soup = contentDriver.requestParsed()
        content_soup = soup.select(contents)
        if content_soup != []:
            try:
                for c in content_soup:
                    if type(hlSel) == list:
                        hl = c.attrs[hlSel[0]] if len(hlSel) < 2 else c.select(hlSel[0])[0].attrs[hlSel[1]]
                    else:
                        hl = c.select(hlSel)[0].text
                    ln = c.attrs[linkSel[0]] if len(linkSel) < 2 else c.select(linkSel[0])[0].attrs[linkSel[1]]
                    img = c.attrs[imgSel[0]] if len(imgSel) < 2 else c.select(imgSel[0])[0].attrs[imgSel[1]]

                    if 'background' in img:
                        img = parse_qs(urlparse(img[img.find("(")+1:img.find(")")]).query)['url'][0] # hack to extract revcontent img urls
                    if 'trends.revcontent' in ln:
                        ln = _getFullURL(ln)


                    output.append({'headline':hl, 'link':ln, 'img':img, "provider":provider, "source":source})
            except Exception as e:
                print("Could not get contents of these native ads on {0} - {1}: {2}".format(source, article, e))
        else:
            print("content soup was empty for {} - {}. Saving a screenshot".format(source, article))
            # save screenshot
            contentDriver.screenshot(source) 
            continue
    return output

def clearDupes(content):
    '''Given some content that we received, we want to remove dupes
    
    Dupes will be defined as both having the same content from the same site
    For now we'll keep same content, different site.
    
    Arguments:
        content {List of lists} -- A list of lists of dictionaries describing a site and the content returned from it
    '''
    cleanContent = []
    for c in content:
        print('incoming dump of {} items'.format(str(len(c))))
        print('removing dupes')
        deduped = list({i['link']:i for i in c}.values())
        print('outgoing only {} items'.format(str(len(deduped))))

        cleanContent.append(deduped)

    return cleanContent

def downloadImages(content):
    '''Taking out cleaned content payload to download all the images locally
    
    For each entry that will go in our database, take the image url, make a hash of that (md5 is fine?)
    and then we'll download the image by that name and add that image id to the record and return the whole thing as a flatmapped list
    
    Arguments:
        content {List of lists} -- A list of lists of dictionaries describing a site and the content returned from it
    '''
    imagedContent = []
    for c in content:
        if c != []:
            print("Attempting to download {} images from {} via {}".format(str(len(c)), c[0]['provider'], c[0]['source']))
            for i in c:

                img_url = i['img']
                img_id = hashlib.sha1(img_url.encode('utf-8')).hexdigest()
                try: 
                    r = requests.get(img_url)
                except Exception as e:
                    print("Getting images for {} failed: {}".format(img_url, e))
                img_format = _getImgFormat(img_url, r.headers.get('Content-Type', ''))
                path = './imgs/{}{}'.format(img_id, img_format)
                if r.status_code == 200:
                    with open(path, 'wb') as imgbuffer:
                        for chunk in r:
                            imgbuffer.write(chunk)
                    i['img_file'] = path

                else:
                    print("count not download image for {}".format(img_url))
                    i['img_file'] = ''

                
                imagedContent.append(i)
        else:
            print("Nothing to add here because it didn't get anything from the source.")

    return imagedContent

def finalizeRecords(records):
    finalRecords = []
    for r in records:
        # add date before we store
        r['date'] = datetime.datetime.utcnow()

        #get the final url for each link (if redirect)
        r['final_link'] = _getFinalURL(r['link'])
        finalRecords.append(r)
    return finalRecords

    
class SessionManager(object):
    """A class for managing Selenium Driver sessions.


    """

    def __init__(self,
                 userAgent="Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2228.0 Safari/537.36",
                 dcap=dict(DesiredCapabilities.PHANTOMJS),
                 driver=None,
                 host='',
                 bwidth=1400,
                 bheight=1000,
                 #timeout=30000,
                 logPath="./logs/ghostdriver_{0}_{1}.log",
                 ssPath="./screenshots/"):
        super(SessionManager, self).__init__()
        self.userAgent = userAgent
        self.dcap = dcap
        self.logPath = logPath.format(host, str(int(time.time())))
        self.ssPath=ssPath
        self.dcap['phantomjs.page.settings.userAgent'] = userAgent
        #self.dcap['phantomjs.page.settings.resourceTimeout'] = timeout
        self.driver = webdriver.PhantomJS(
            desired_capabilities=self.dcap, 
            service_log_path=self.logPath,
            service_args=['--ignore-ssl-errors=true',
                          '--debug=true',
                          '--load-images=false'
                        ])
        self.driver.set_window_size(bwidth,bheight)

    def __del__(self):
        self.driver.quit()

    def requestParsed(self, html=None):
        """Method to input html and get out a BeautifulSoup Parser

        Provide the driver.page_source in your SessionManager to get
        back a parser to navigate the HTML returned from your request

        Keyword Arguments:
                html {[selenium webdriver page_source]} -- [html page source returned from selenium webdriver.page_source] (default: {driver.page_source})

        Returns:
                [class 'bs4.BeautifulSoup'] -- [html parsed by beautiful soup]
        """

        self.html = self.driver.page_source
        return BeautifulSoup(self.html, 'html.parser')

    def screenshot(self, source=None):
        """Method to take a screenshot of whatever page drive is on when it fails

        Provide the driver a source (whatever provider it is getting content from) to SessionManager to 
        create a path and save a screen shot of curren page to.

        Keyword Arguments:
                source {[str]} -- String signifying what provider (website) the failed page is on.

        Returns:
                [screenshot] -- saves image of page to directory ('screenshots')
        """
        source = source.split('/')[-1]
        filename = "{}_{}.png".format(source, str(int(time.time())))
        savePath = os.path.join(self.ssPath, filename)
        print('saving screen shot to {}'.format(savePath))
        return self.driver.save_screenshot(savePath)


if __name__ == '__main__':

    '''Args'''
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', '--test', action='store_true', help='run in test mode or not')
    parser.add_argument('-r', '--resource', help='where to get the site crawl list from', required=True)
    args = parser.parse_args()

    RESOURCES = args.resource
    WORKERS_MAX = 5
    targets = fetchSiteGuide(RESOURCES)
    MONGO = c.MongoConn('theothercontent', 'contents')

    #use workers to grab new articles
    ap = Pool(WORKERS_MAX)
    articleResults = ap.map(getArticles, targets)
    ap.close()

    # join articles to target output so we have a single package to send for content
    for articles in articleResults:
        for target in targets:
            if target['site'] == list(articles.keys())[0]:
                target['articles'] = articles[target['site']]


    # now use workers to grab content data from each article
    ctp = Pool(WORKERS_MAX)
    contentResults = ctp.map(getArticleData, targets)
    ctp.close()
    # now that we have everything, let's remove duplicates before going any further
    forImaging = clearDupes(contentResults)

    # next lets create a hash for each img location and use that as a filename for the image we'll store, and add the hash on the record
    withImages = downloadImages(contentResults)

    # finally wrap up with final details for storing
    forStorage = finalizeRecords(withImages)

    print(forStorage)   

    # and store it
    if not args.test:
        MONGO.save_records(forStorage)




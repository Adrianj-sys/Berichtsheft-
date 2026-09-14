#!/usr/bin/env python3
'''Form fill: delete all entries, then upload fresh ones via Playwright.'''

import sys, json, time, pickle
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = 'https://www.azubiheft.de'
DAY_ORDER = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag']
COOKIE_FILE = Path(__file__).parent.parent / 'auth' / 'session.pkl'


def fill_report(report_nr, json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        entries = json.load(f)
    
    print(f'Report {report_nr}: {len(entries)} days')
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        
        if COOKIE_FILE.exists():
            with open(COOKIE_FILE, 'rb') as f:
                cookies = pickle.load(f)
            page.goto(BASE_URL, wait_until='domcontentloaded', timeout=10000)
            for c in cookies:
                page.context.add_cookies([{'name': c.name, 'value': c.value, 'domain': '.azubiheft.de', 'path': '/'}])
        
        try:
            page.goto(f'{BASE_URL}/Azubi/Default.aspx', wait_until='domcontentloaded', timeout=10000)
        except:
            pass
        
        if 'Login' in page.url:
            print('Login needed')
            page.goto(f'{BASE_URL}/Login.aspx')
            input('Press Enter after login...')
        
        weekly_url = f'{BASE_URL}/Azubi/Wochenansicht.aspx?NachweisNr={report_nr}'
        
        print('Deleting existing entries...')
        for day_name in DAY_ORDER:
            if day_name not in entries:
                continue
            page.goto(weekly_url, wait_until='networkidle', timeout=15000)
            time.sleep(2)
            day_divs = page.locator('div.mo')
            for i in range(day_divs.count()):
                if day_name in day_divs.nth(i).inner_text():
                    day_divs.nth(i).click()
                    page.wait_for_load_state('networkidle', timeout=10000)
                    time.sleep(1)
                    break
            while True:
                entry_rows = page.locator('div.d0.mo')
                if entry_rows.count() <= 1:
                    break
                try:
                    entry_rows.nth(1).click()
                    time.sleep(0.5)
                    page.click('#cmdDel')
                    time.sleep(0.5)
                    page.click('#cmdConfirmBoxOK')
                    time.sleep(1)
                except:
                    break
            print(f'  {day_name}: cleared')
        
        print('Uploading new entries...')
        page.goto(weekly_url, wait_until='networkidle', timeout=15000)
        time.sleep(2)
        for day_name in DAY_ORDER:
            if day_name not in entries:
                continue
            activities = entries[day_name]
            print(f'{day_name}: {len(activities)} entries')
            day_divs = page.locator('div.mo')
            for i in range(day_divs.count()):
                if day_name in day_divs.nth(i).inner_text():
                    day_divs.nth(i).click()
                    page.wait_for_load_state('networkidle', timeout=10000)
                    time.sleep(1)
                    break
            for activity in activities:
                try:
                    page.wait_for_selector('#cmdNeue', state='visible', timeout=5000)
                    page.click('#cmdNeue')
                    time.sleep(0.5)
                    page.click('#txtTaetigkeit')
                    page.keyboard.type(activity['task'])
                    time.sleep(0.3)
                    page.click('#cmdDauer')
                    time.sleep(0.3)
                    hours_int = int(activity['hours'])
                    mins = int((activity['hours'] - hours_int) * 60)
                    time_str = f'{hours_int:02d}{mins:02d}'
                    for digit in time_str:
                        page.click(f'.Num.mo:text-is(\'{digit}\')')
                        time.sleep(0.1)
                    page.click('#cmdNumOk')
                    time.sleep(0.3)
                    page.click('#divOK')
                    time.sleep(0.5)
                except Exception as e:
                    print(f'  Error: {e}')
            page.goto(weekly_url, wait_until='networkidle', timeout=15000)
            time.sleep(1)
        
        print('Done!')
        browser.close()


if __name__ == '__main__':
    fill_report(int(sys.argv[1]), sys.argv[2])

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

STORE_URL = 'https://sunbelx.com/store/20'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; BelxMeshiBot/1.0)'}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def find_pdf(html):
    text = html.decode('utf-8', 'ignore')
    # Store pages may contain older flyer links before the visible "PDF版チラシ" button.
    links = re.findall(r"""(?:href|src)=["']([^"']+\.pdf(?:\?[^"']*)?)["']""", text, re.I)
    candidates = []
    for link in links:
        url = urllib.parse.urljoin(STORE_URL, link)
        if 'sunbelx.com/upload/Flyer/' in url:
            candidates.append(url)
    if not candidates:
        raise RuntimeError('official flyer PDF link not found')
    # The current flyer link is usually the final matching link in the store page.
    return candidates[-1]


def ocr_pdf(pdf_path, outdir):
    # Prefer embedded text; log each stage and bound OCR runtime.
    txt_path = os.path.join(outdir, 'embedded.txt')
    print('Step 1/3: trying embedded PDF text', flush=True)
    try:
        subprocess.run(['pdftotext', '-layout', pdf_path, txt_path], check=True, timeout=12)
        with open(txt_path, encoding='utf-8', errors='ignore') as f:
            embedded = f.read()
        if len(embedded) > 100 and len(re.findall(r'[ぁ-んァ-ヶ一-龠]', embedded)) > 30:
            print(f'Embedded text found ({len(embedded)} characters).', flush=True)
            return embedded
        print('No usable embedded text; switching to OCR.', flush=True)
    except (subprocess.SubprocessError, OSError) as e:
        print(f'Embedded text extraction unavailable: {e}; switching to OCR.', flush=True)

    prefix = os.path.join(outdir, 'page')
    print('Step 2/3: rendering up to first 4 pages at 180 dpi', flush=True)
    subprocess.run(['pdftoppm', '-f', '1', '-l', '4', '-jpeg', '-jpegopt', 'quality=88', '-r', '180', pdf_path, prefix], check=True, timeout=30)
    pages = sorted(x for x in os.listdir(outdir) if x.startswith('page-') and x.endswith('.jpg'))
    if not pages:
        raise RuntimeError('PDF render produced no pages')
    texts = []
    for i, page in enumerate(pages, 1):
        p = os.path.join(outdir, page)
        print(f'Step 3/3: OCR page {i}/{len(pages)}', flush=True)
        try:
            result = subprocess.run(
                ['tesseract', p, 'stdout', '-l', 'jpn+eng', '--psm', '6'],
                capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=45
            )
        except subprocess.TimeoutExpired:
            print(f'OCR page {i} exceeded 45 seconds; skipping page.', flush=True)
            continue
        if result.returncode == 0 and result.stdout.strip():
            texts.append(result.stdout)
        else:
            print(f'OCR returned no text for page {i}.', flush=True)
    return '\n'.join(texts)


def price_items(text):
    patterns = [
        ('豚こま', r'豚(?:肉)?こま[^\n]{0,40}?(\d{2,4})円'),
        ('豚肉', r'豚肉[^\n]{0,40}?(\d{2,4})円'),
        ('鶏もも肉', r'若鶏(?:モモ|もも)肉[^\n]{0,40}?(\d{2,4})円'),
        ('鶏むね肉', r'若鶏(?:ムネ|むね)肉[^\n]{0,40}?(\d{2,4})円'),
        ('牛肉', r'牛肉[^\n]{0,40}?(\d{2,4})円'),
        ('鮭', r'(?:鮭|サーモン)[^\n]{0,40}?(\d{2,4})円'),
        ('ぶり', r'ぶり[^\n]{0,40}?(\d{2,4})円'),
        ('白菜', r'白菜[^\n]{0,40}?(\d{2,4})円'),
        ('キャベツ', r'キャベツ[^\n]{0,40}?(\d{2,4})円'),
        ('長ねぎ', r'長ねぎ[^\n]{0,40}?(\d{2,4})円'),
        ('ほうれん草', r'ほうれん草[^\n]{0,40}?(\d{2,4})円'),
        ('なす', r'なす[^\n]{0,40}?(\d{2,4})円'),
        ('卵', r'(?:たまご|卵)[^\n]{0,40}?(\d{2,4})円'),
        ('豆腐', r'豆腐[^\n]{0,40}?(\d{2,4})円'),
        ('納豆', r'納豆[^\n]{0,40}?(\d{2,4})円'),
    ]
    found = []
    for name, pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            found.append({'name': name, 'price': int(m.group(1))})
    if not found:
        # OCR sometimes drops the yen sign and splits price from product name.
        compact = re.sub(r'\s+', '', text)
        labels = [('豚こま', '豚こま'), ('豚肉', '豚肉'), ('鶏もも肉', '鶏もも'), ('鶏むね肉', '鶏むね'), ('牛肉', '牛肉'), ('鮭', '鮭'), ('ぶり', 'ぶり'), ('白菜', '白菜'), ('キャベツ', 'キャベツ'), ('長ねぎ', '長ねぎ'), ('ほうれん草', 'ほうれん草'), ('なす', 'なす'), ('卵', 'たまご|卵'), ('豆腐', '豆腐'), ('納豆', '納豆')]
        for name, label in labels:
            m = re.search(label + r'.{0,100}?(\d{2,4})', compact, re.I)
            if m and 10 <= int(m.group(1)) <= 5000:
                found.append({'name': name, 'price': int(m.group(1))})
    return found


def recipes(items):
    names = {x['name'] for x in items}
    out = []
    def add(title, use, extra, steps):
        out.append({'title': title, 'use': use, 'extra': extra, 'steps': steps})
    if '豚こま' in names or '豚肉' in names:
        add('豚こま野菜炒め', '豚こま＋キャベツ・長ねぎ等', ['もやし', '焼肉のたれ'], '豚肉を炒め、野菜を加えて火を通し、焼肉のたれで仕上げる。')
    if '鶏もも肉' in names or '鶏むね肉' in names:
        add('鶏の照り焼き丼', '鶏肉＋長ねぎ等', ['醤油', 'みりん', '砂糖', 'ご飯'], '鶏肉を焼き、醤油・みりん・砂糖を加えて煮絡め、ご飯にのせる。')
    if '鮭' in names or 'ぶり' in names:
        add('魚の照り焼き定食', '鮭・ぶり等＋野菜', ['醤油', 'みりん', 'ご飯'], '魚を焼き、醤油・みりんを加えて照りが出るまで煮絡める。')
    if '牛肉' in names:
        add('牛肉と野菜の甘辛炒め', '牛肉＋野菜', ['醤油', '砂糖', 'ご飯'], '牛肉と野菜を炒め、醤油と砂糖で甘辛く味付けする。')
    if '卵' in names and ('豚こま' in names or '豚肉' in names):
        add('豚たま丼', '豚肉＋卵＋長ねぎ', ['めんつゆ', 'ご飯'], '豚肉とねぎを煮て、溶き卵を回し入れて半熟で止め、ご飯にのせる。')
    if '豆腐' in names and ('豚こま' in names or '豚肉' in names):
        add('豚肉豆腐', '豚肉＋豆腐', ['めんつゆ', '長ねぎ'], '豚肉を煮て豆腐を加え、めんつゆで味を整える。')
    while len(out) < 3:
        add('特売肉と野菜の簡単炒め', '特売の肉＋特売野菜', ['塩こしょう', '醤油', 'ご飯'], '肉を炒め、野菜を加え、塩こしょうと醤油で味付けする。')
    return out[:3]


def main():
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    with tempfile.TemporaryDirectory() as td:
        html = fetch(STORE_URL)
        pdf_url = find_pdf(html)
        pdf = os.path.join(td, 'flyer.pdf')
        with open(pdf, 'wb') as f:
            f.write(fetch(pdf_url))
        text = ocr_pdf(pdf, td)
        # Search all extracted flyer text. Date OCR is unreliable and can crop out the sale listings.
        items = price_items(text)
        print(f'Flyer PDF: {pdf_url}')
        print(f'OCR preview: {text[:1200]}')
        if not items:
            data = {'date': today.isoformat(), 'source': 'ベルクス戸田店 公式チラシ', 'source_url': pdf_url, 'status': 'ocr_no_items', 'sale_items': [], 'recipes': [], 'shopping': [], 'updated_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec='minutes')}
            with open('data.json', 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print('No reliable sale items found; did not invent prices.')
            return
        data = {
            'date': today.isoformat(),
            'source': 'ベルクス戸田店 公式チラシ',
            'source_url': pdf_url,
            'sale_items': items,
            'recipes': recipes(items),
            'shopping': sorted(set(sum(([x for x in r['extra']] for r in recipes(items)), []))),
            'updated_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec='minutes')
        }
        with open('data.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

if __name__ == '__main__':
    main()

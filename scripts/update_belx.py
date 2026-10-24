"""ベルクス戸田店の公式チラシPDFを取得し、特売品の読み取りとレシピ3品の提案を行う。

読み取りは Claude API（画像入力）を使う。環境変数 ANTHROPIC_API_KEY が無い場合は
Tesseract による従来の OCR にフォールバックする（精度は低い）。
"""
import base64
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

STORE_URL = 'https://sunbelx.com/store/20'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; BelxMeshiBot/1.0)'}
JST = dt.timezone(dt.timedelta(hours=9))
MODEL = os.environ.get('CLAUDE_MODEL', 'claude-sonnet-5-5')
MAX_PAGES = 4

PROMPT = """添付画像は、埼玉県戸田市「ベルクス 戸田店」の今日のチラシ（PDFの各ページ）です。今日は {today} です。

1. 食材の特売品を読み取ってください（肉・魚・野菜・果物・卵・豆腐・乳製品・米・麺・調味料など料理に使うもの）。
   日用品・お酒・お菓子・飲料は除外。価格が読めないものは price を null に。
   日付指定のある商品は、今日 {today} が対象期間に入るものだけを入れてください。
2. 読み取った特売品を中心に、2人分の晩ごはん向けレシピを3品提案してください。
   主菜2品＋副菜か汁物1品の組み合わせにし、特売品をできるだけ多く、無駄なく使うこと。
   基本調味料（塩・こしょう・醤油・砂糖・みりん・酒・味噌・油・ごま油・片栗粉・鶏がらスープの素）は家にある前提。
3. shopping には、3品を作るのに特売品以外で買い足しが必要なものだけを入れてください。

結果は save_result ツールで保存してください。各項目の形は次の例のとおりです。
{{"sale_items":[{{"name":"国産豚こま切れ","price":98,"unit":"100g","note":"お一人様2点まで"}}],
 "recipes":[{{"name":"料理名","minutes":20,"uses":["豚こま","キャベツ"],"reason":"一言でおすすめ理由",
   "ingredients":["豚こま切れ 200g"],"steps":["手順1","手順2"]}}],
 "shopping":["もやし"]}}
recipes は必ず3件、steps は3〜6個。"""


def retry(label, fn, tries=3, wait=20):
    """一時的なエラー（通信・混雑・返答の崩れ）は少し待ってやり直す。"""
    for i in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            print(f'{label}: attempt {i}/{tries} failed: {e}', flush=True)
            if i == tries:
                raise
            time.sleep(wait * i)

STR = {'type': 'string'}
RESULT_TOOL = {
    'name': 'save_result',
    'description': 'チラシから読み取った特売品と、それを使った晩ごはんレシピ3品を保存する。',
    'input_schema': {
        'type': 'object',
        'properties': {
            'sale_items': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'name': STR, 'price': {'type': ['integer', 'null']}, 'unit': STR, 'note': STR},
                'required': ['name']}},
            'recipes': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'name': STR, 'minutes': {'type': 'integer'}, 'uses': {'type': 'array', 'items': STR},
                'reason': STR, 'ingredients': {'type': 'array', 'items': STR},
                'steps': {'type': 'array', 'items': STR}},
                'required': ['name', 'ingredients', 'steps']}},
            'shopping': {'type': 'array', 'items': STR},
        },
        'required': ['sale_items', 'recipes', 'shopping'],
    },
}


def fetch(url):
    def go():
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    return retry(f'fetch {url}', go)


def find_pdf(html):
    text = html.decode('utf-8', 'ignore')
    links = re.findall(r"""(?:href|src)=["']([^"']+\.pdf(?:\?[^"']*)?)["']""", text, re.I)
    candidates = []
    for link in links:
        url = urllib.parse.urljoin(STORE_URL, link)
        if 'sunbelx.com/upload/Flyer/' in url and url not in candidates:
            candidates.append(url)
    if not candidates:
        raise RuntimeError('official flyer PDF link not found')
    print(f'Flyer PDF candidates: {candidates}', flush=True)
    # 店舗ページでは古いリンクが先に出ることがあるため、最後のものを採用する。
    return candidates[-1]


def render_pages(pdf_path, outdir, dpi):
    prefix = os.path.join(outdir, f'page{dpi}')
    subprocess.run(
        ['pdftoppm', '-f', '1', '-l', str(MAX_PAGES), '-jpeg', '-jpegopt', 'quality=85',
         '-r', str(dpi), pdf_path, prefix],
        check=True, timeout=60,
    )
    pages = sorted(
        os.path.join(outdir, x) for x in os.listdir(outdir)
        if x.startswith(f'page{dpi}-') and x.endswith('.jpg')
    )
    if not pages:
        raise RuntimeError('PDF render produced no pages')
    return pages


def parse_json(text):
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < 0:
        raise ValueError('no JSON object in reply')
    return json.loads(text[start:end + 1])


def ask_claude(pages, today, api_key):
    content = []
    for p in pages:
        with open(p, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': b64}})
    content.append({'type': 'text', 'text': PROMPT.format(today=today.isoformat())})
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 8000,
        'tools': [RESULT_TOOL],
        'tool_choice': {'type': 'tool', 'name': RESULT_TOOL['name']},
        'messages': [{'role': 'user', 'content': content}],
    }).encode()
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages', data=body, method='POST',
        headers={'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
    )
    def call():
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'Claude API error {e.code}: {e.read().decode(errors="ignore")[:500]}')
        if res.get('stop_reason') == 'max_tokens':
            raise RuntimeError('reply was cut off (max_tokens)')
        # 結果はツール呼び出しの形で受け取るので、JSONの書式崩れが起きない
        for b in res.get('content', []):
            if b.get('type') == 'tool_use' and isinstance(b.get('input'), dict):
                return b['input']
        reply = ''.join(b.get('text', '') for b in res.get('content', []) if b.get('type') == 'text')
        return parse_json(reply)
    data = retry('Claude API', call, tries=3, wait=30)
    items = [x for x in data.get('sale_items', []) if isinstance(x, dict) and x.get('name')]
    recipes = [x for x in data.get('recipes', []) if isinstance(x, dict) and x.get('name')][:3]
    shopping = [str(x) for x in data.get('shopping', []) if x]
    return items, recipes, shopping


def tesseract_text(pages):
    texts = []
    for i, p in enumerate(pages, 1):
        try:
            r = subprocess.run(['tesseract', p, 'stdout', '-l', 'jpn+eng', '--psm', '11'],
                               capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=40)
            texts.append(r.stdout)
        except subprocess.TimeoutExpired:
            print(f'OCR page {i} timed out', flush=True)
    return '\n'.join(texts)


def fallback_items(text):
    """APIキーが無いときの簡易抽出。代表的な食材名の近くにある価格だけ拾う。"""
    labels = ['豚こま', '豚肉', '鶏もも', '鶏むね', '牛肉', 'ひき肉', '鮭', 'ぶり', 'さば', '白菜', 'キャベツ',
              '長ねぎ', '玉ねぎ', 'じゃがいも', 'にんじん', 'ほうれん草', 'もやし', 'なす', 'たまご', '卵', '豆腐', '納豆']
    compact = re.sub(r'\s+', '', text)
    found = []
    for label in labels:
        m = re.search(re.escape(label) + r'.{0,40}?(\d{2,4})円?', compact)
        if m and 10 <= int(m.group(1)) <= 3000:
            found.append({'name': label, 'price': int(m.group(1)), 'unit': '', 'note': 'OCR推定'})
    return found


def write(data):
    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')


def previous():
    try:
        with open('data.json', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    now = dt.datetime.now(JST)
    today = now.date()
    prev = previous()
    if prev.get('date') == today.isoformat() and prev.get('status') == 'ok' and os.environ.get('GITHUB_EVENT_NAME') == 'schedule':
        print('Today is already updated; skipping this scheduled run.', flush=True)
        return
    try:
        run(now, today)
    except Exception as e:
        # 失敗しても前回の内容は残し、失敗したことだけ記録する
        print(f'Update failed: {e}', flush=True)
        prev = prev or {'sale_items': [], 'recipes': [], 'shopping': []}
        prev['last_error'] = f'{now.isoformat(timespec="minutes")} {str(e)[:300]}'
        write(prev)
        raise


def run(now, today):
    base = {
        'date': today.isoformat(),
        'source': 'ベルクス戸田店 公式チラシ',
        'source_url': None,
        'status': 'ok',
        'sale_items': [],
        'recipes': [],
        'shopping': [],
        'updated_at': now.isoformat(timespec='minutes'),
    }
    with tempfile.TemporaryDirectory() as td:
        pdf_url = find_pdf(fetch(STORE_URL))
        base['source_url'] = pdf_url
        pdf = os.path.join(td, 'flyer.pdf')
        with open(pdf, 'wb') as f:
            f.write(fetch(pdf_url))

        api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        if api_key:
            # 1ページあたり約1.2MPに収まる解像度。細かい価格表示が読める範囲で小さめにする。
            pages = render_pages(pdf, td, 110)
            print(f'Sending {len(pages)} page(s) to {MODEL}', flush=True)
            items, recipes, shopping = ask_claude(pages, today, api_key)
            base.update(sale_items=items, recipes=recipes, shopping=shopping, reader='claude')
        else:
            print('ANTHROPIC_API_KEY is not set; falling back to Tesseract OCR.', flush=True)
            pages = render_pages(pdf, td, 200)
            items = fallback_items(tesseract_text(pages))
            base.update(sale_items=items, reader='tesseract')

    if not base['sale_items']:
        base['status'] = 'no_items'
    elif not base['recipes']:
        base['status'] = 'no_recipes'
    print(json.dumps({k: base[k] for k in ('status', 'sale_items')}, ensure_ascii=False)[:1500], flush=True)
    write(base)


if __name__ == '__main__':
    main()

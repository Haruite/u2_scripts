# python3.6及以上
# 依赖：pip3 install PyYAML requests bs4 deluge-client qbittorrent-api loguru pytz

import os
import yaml
import qbittorrentapi
import pytz

from bs4 import BeautifulSoup
from datetime import datetime
from time import time, sleep
from loguru import logger
from requests import request
from concurrent.futures import ThreadPoolExecutor, as_completed
from deluge_client import FailedToReconnectException, LocalDelugeRPCClient
from qbittorrentapi.exceptions import APIConnectionError, HTTPError
from requests.exceptions import ReadTimeout

LocalDelugeRPCClient.timeout = 10

# *************************必填配置************************
clients_info = '''  # 按 yaml 语法填写客户端信息
-  # 可以填写多个客户端
    type: deluge  # de, deluge
    host: 127.0.0.1  # IP
    port: 58846  # daemon 端口
    username:   # 本地可以设置跳过用户名和密码
    password:   # cat ~/.config/deluge/auth
- 
    type: qbittorrent  # qb, qbittorrent
    host: http://127.0.0.1  # IP
    port: 8080  # webui 端口
    username:   # web 用户名
    password:   # web 密码
'''
cookie = 'nexusphp_u2='

# ************************可修改配置***********************
min_rate = 360  # 最小上传速度(KiB/s)
min_size = 5  # 最小体积(GiB)
min_d = 180  # 种子最小生存天数
uc_max = 30000  # 单个魔法最大 uc 使用量
total_uc_max = 600000  # 24h 内 uc 最大使用量
interval = 120  # 检查的间隔
data_path = f'{os.path.splitext(__file__)[0]}.data.txt'  # 数据路径
log_path = f'{os.path.splitext(__file__)[0]}.log'  # 日志路径
logger.add(level='DEBUG', sink=log_path, encoding='utf-8', rotation="5 MB")  # 日志设置
proxies = {  # 代理
           # 'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'
}
headers = {
    'authority': 'u2.dmhy.org',
    'accept-encoding': 'gzip, deflate',
    'accept-language': 'zh-CN,zh;q=0.8',
    'cookie': cookie,
    'referer': 'https://u2.dmhy.org/index.php',
    'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/99.0.4814.0 Safari/537.36 Edg/99.0.1135.6'
}

# *************************END****************************


def write(name):
    if name in globals():
        with open(data_path, 'r', encoding='utf-8') as f1, \
                open(f'{data_path}.bak', 'w', encoding='utf-8') as f2:
            k = 0
            for line in f1:
                if f'{name} = ' in line and '__' not in line:
                    k = 1
                    f2.write(f'{name} = {globals()[name]}\n')
                else:
                    f2.write(line)
            if k == 0:
                f2.write(f'{name} = {globals()[name]}\n')
        os.remove(data_path)
        os.rename(f'{data_path}.bak', data_path)


def rq(method, url, headers=headers, proxies=proxies, timeout=10, retries=5, **kw):
    for i in range(retries):
        try:
            html = request(method, url=url, headers=headers, proxies=proxies, timeout=timeout, **kw)
            code = html.status_code
            if code < 400:
                if method == 'get':
                    logger.debug(f'Downloaded page: {url}')
                return html
            elif i == retries - 1:
                raise Exception(f'Failed to request... method: {method}, url: {url}, kw: {kw}'
                                f' ------ status code: {html.status_code}')
            elif code in [502, 503]:
                delay = int(html.headers.get('Retry-After') or '30')
                logger.error(f'Will attempt to request after {delay}s')
                sleep(delay)
        except Exception as e:
            if i == retries - 1:
                raise
            elif isinstance(e, ReadTimeout):
                timeout += 20


def to_info(client):
    if isinstance(client, LocalDelugeRPCClient):
        keys = ['name', 'tracker', 'total_size', 'upload_payload_rate', 'state']
        if not client.connected:
            for i in range(5):
                try:
                    client.reconnect()
                    logger.debug(f'Connected to deluge host ------ {client.host}')
                    break
                except:
                    sleep(0.3 * 2 ** i)
        try:
            return client.call('core.get_torrents_status', {'state': 'Active'}, keys)
        except FailedToReconnectException:
            logger.error(f'Failed to reconnect to deluge client! Host  -------  {client.host}')

    if isinstance(client, qbittorrentapi.Client):
        torrents_info = {}
        try:
            for torrent in client.torrents_info(status_filter=['active'], _retries=5):
                _id = torrent['hash']
                data = {'name': torrent['name'],
                        'tracker': torrent['tracker'],
                        'total_size': torrent['size'],
                        'upload_payload_rate': torrent['upspeed'],
                        'state': torrent['state']
                        }
                torrents_info[_id] = data
            return torrents_info
        except HTTPError as e:
            logger.error(f'Failed to connect to qbittorrent on {client.host} '
                         f'due to http error: {e}')
        except APIConnectionError as e:
            logger.error(f'qbittorrentapi.exceptions.APIConnectionError:  {e}')


def magic_list(torrents_info):
    ids = []
    for _id, data in torrents_info.items():
        if _id in magic_info:
            if int(time()) - magic_info[_id]['ts'] < 86400:  # 魔法还在有效期内则不加入
                continue
        if not data['tracker'] or 'daydream.dmhy.best' not in data['tracker'] and \
                'tracker.dmhy.org' not in data['tracker']:  # 过滤不是 U2 的种子
            continue
        if data['state'] in ['Downloading', 'downloading']:  # 过滤下载中的种子
            pass
            # continue
        if data['upload_payload_rate'] < min_rate * 1024:
            continue
        if data['total_size'] < min_size * 1024 ** 3:
            continue
        url = f'https://u2.dmhy.org/torrents.php?incldead=0&spstate=0' \
              f'&inclbookmarked=0&search={_id}&search_area=5&search_mode=0'
        soup = BeautifulSoup(rq('get', url).text.replace('\n', ''), 'lxml')
        tz_info = soup.find('a', {'href': 'usercp.php?action=tracker#timezone'})['title']
        pre_suf = [['时区', '，点击修改。'], ['時區', '，點擊修改。'], ['Current timezone is ', ', click to change.']]
        tz = [tz_info[len(pre):][:-len(suf)].strip() for pre, suf in pre_suf if tz_info.startswith(pre)][0]
        table = soup.select('table.torrents')

        if table:
            cont = table[0].contents[1].contents
            date = cont[3].time.attrs.get('title') or cont[3].time.get_text(' ')
            dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
            delta = time() - pytz.timezone(tz).localize(dt).timestamp()
            if delta < min_d * 86400:
                magic_info[_id] = {'ts': int(time())}
                write('magic_info')
                continue
            tid = int(cont[1].a['href'][15:-6])
            ids.append((_id, tid,))
            l = len(ids)
            for img in cont[1].select('tr')[1].td.select('img') or []:
                if img.get('class') == ['arrowup'] and img.next_element.text[:-1].replace(',', '.') == '2.33':
                    logger.debug(f'Torrent {_id}, id: {tid}: 2.33x uploaded magic existed!')
                    pro_end_date = cont[1].time.get('title') or cont[1].time.text
                    end_time = datetime.strptime(pro_end_date, '%Y-%m-%d %H:%M:%S')
                    magicst = int(pytz.timezone(tz).localize(end_time).timestamp()) - 86400
                    magic_info[_id] = {'ts': magicst}
                    write('magic_info')
                    ids = ids[:(l - 1)]

        else:
            logger.error(f'Torrent {_id} , name: {data["name"]} not founded in site...')
            magic_info[_id] = {'ts': int(time()) + 86400 * 3}
            write('magic_info')

    return ids


def magic(ids):
    tuc = 0
    for _id in list(magic_info.keys()):
        data = magic_info[_id]
        if int(time()) - data['ts'] < 86400:
            if 'uc' in data:
                tuc += data['uc']
        else:
            del magic_info[_id]

    for _id, tid in ids:
        url = f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}'
        soup = BeautifulSoup(rq('get', url).text, 'lxml')
        data = {h['name']: h['value'] for h in soup.find_all('input', {'type': 'hidden'})}
        data.update({'user_other': '', 'start': 0, 'promotion': 8, 'comment': ''})
        data.update({'user': 'SELF', 'hours': 24, 'ur': 2.33, 'dr': 1})

        try:
            response = eval(rq('post', 'https://u2.dmhy.org/promotion.php?test=1', data=data).text)
            if response['status'] == 'operational':
                uc = int(float(BeautifulSoup(response['price'], 'lxml').span['title'].replace(',', '')))

                if uc > uc_max:
                    logger.warning(f'Torrent id: {tid} cost {uc}uc, too expensive')
                    magic_info[_id] = {'ts': int(time())}
                    write('magic_info')
                    continue

                tuc += uc
                if tuc > total_uc_max:
                    logger.warning('24h ucoin usage exceeded, Waiting ------')
                    return

                url = f'https://u2.dmhy.org/promotion.php?action=magic&torrent={tid}'
                p2 = rq('post', url, retries=1, data=data)
                if p2.status_code == 200:
                    logger.info(f'Sent magic to torrent {_id}, tid: {tid}. Ucoin usage {uc}, 24h total usage {tuc}')
                    magic_info[_id] = {'ts': int(time()), 'uc': uc}
                    write('magic_info')
                else:
                    logger.error(f'Failed to send magic to torrent {_id}, '
                                 f'id: {tid} ------ status code: {p2.status_code}')

        except Exception as e:
            logger.exception(e)


def main(client):
    while True:
        try:
            torrents_info = to_info(client)
            if torrents_info is not None:
                ids = magic_list(torrents_info)
                magic(ids)
        except Exception as e:
            logger.exception(e)
        finally:
            sleep(interval)


def init():
    c_info = yaml.load(clients_info, yaml.FullLoader)
    _clients = []
    _info = {}

    for c in c_info:
        t = c['type']
        del c['type']
        if t in ['de', 'Deluge', 'deluge']:
            c['decode_utf8'] = True
            _clients.append(LocalDelugeRPCClient(**c))
        if t in ['qb', 'QB', 'qbittorrent', 'qBittorrent']:
            c['REQUESTS_ARGS'] = {'timeout': 10}
            c['FORCE_SCHEME_FROM_HOST'] = True
            _clients.append(qbittorrentapi.Client(**c))

    f = open(data_path, 'a', encoding='utf-8')
    f.close()
    with open(data_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('magic_info'):
                _info = eval(line.lstrip('magic_info = '))

    return _clients, _info


def run():
    try:
        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {executor.submit(main, client): client for client in clients}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    client = futures[future]
                    typename = 'qbittorrent'
                    if isinstance(client, LocalDelugeRPCClient):
                        typename = 'deluge'
                    logger.critical(f'Future for {typename} client on {client.host}:{client.port} terminated. '
                                    f'Check the exception message.')
                    logger.exception(e)
                else:
                    logger.critical(f'Future for {typename} client on {client.host}:{client.port} '
                                    f'terminated without exception')
    except KeyboardInterrupt:
        write('magic_info')
        logger.info(f'This script: {__file__} has been manually terminated.')
        os._exit(0)


if __name__ == '__main__':
    clients, magic_info = init()
    run()

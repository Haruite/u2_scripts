"""deluge 又崩了，把 lt 换成了 1.1.14，但是种子全部报错要重新校验
不想分析 fastresume，写了一个脚本重新添加种子并且跳过校验
需要 deluge 版本 2.x，先不要删种子，按提示操作，如果有异常退出就重新运行
"""

import os
import json
from base64 import b64encode

from loguru import logger
from deluge_client import LocalDelugeRPCClient

# -------------------------------------------------------------
host = '127.0.0.1'
port = 58846
username = ''
password = ''
add_paused = True  # 是否添加种子为暂停状态
set_label = False  # 是否保存种子 label 信息
# -------------------------------------------------------------

logger.add(level='DEBUG', sink=f'{os.path.splitext(__file__)[0]}.log')
client = LocalDelugeRPCClient(host, port, username, password)
client.connect()

if not os.path.exists(f'{os.path.splitext(__file__)[0]}.torrents_info'):
    torrents_info = client.call('core.get_torrents_status', {},
                                [*['total_done', 'total_size', 'download_location', 'name'],
                                 *(['label'] if set_label else [])])
    unfinished_name = [data['name'] for _id, data in torrents_info.items() if data['total_size'] != data['total_done']]
    if unfinished_name:
        i = input(f"These are all unfinished torrents' name: {unfinished_name}. "
                  f"These torrents will be checking hash after added."
                  f"If there is something wrong, input y. Else input n\n")
        while True:
            if i in ['y', 'Y']:
                unfinished_hash = input("Enter hashes of unfinished torrents and join them with space."
                                        "For example: 4dabc0ea569e31f215c459106eef575ecdf553a8 "
                                        "1b6fb46a51eaf8dbb82bef95e95bfb0f529c443f\n").split(' ')
                for _id, data in torrents_info.items():
                    del data['total_size']
                    del data['total_done']
                    data['finished'] = False if _id in unfinished_hash else True
                break
            elif i in ['n', 'N']:
                break
    with open(f'{os.path.splitext(__file__)[0]}.torrents_info', 'w') as fp:
        json.dump(torrents_info, fp)
    print('Now you have to do two things: First copy ~/.config/deluge/state directory'
          '(and all torrents in it) to another place, '
          'Second remove all torrents in deluge client (do not remove data).')
else:
    with open(f'{os.path.splitext(__file__)[0]}.torrents_info', 'r') as fp:
        torrents_info = json.load(fp)

torrent_dir = input('Enter the state directory which you have copied, For example: C:/backup/deluge/state\n')
hashes = client.call('core.get_session_state')
for _id, data in torrents_info.items():
    if _id not in hashes:
        if 'finished' in data:
            seed_mode = data['finished']
        else:
            seed_mode = True if data['total_size'] == data['total_done'] else False
        options = {'seed_mode': seed_mode, 'download_location': data['save_path'], 'add_paused': add_paused}
        with open(f'{torrent_dir}/{_id}.torrent', 'rb') as f:
            client.call('core.add_torrent_file', f"[U2].{data['name']}.torrent", b64encode(f.read()), options)
            logger.info(f"Add torrent {_id} | name {data['name']} | options {options}")
        if 'label' in data and data['label'] != '':
            client.call('label.set_torrent', _id, data['label'])
            logger.info(f"Set torrent {_id} label to {data['label']}")

# 把以前的数据恢复了，fastresume 还是很简单的，前面的脚本就不误导人了

from my_bencoder import bencode, bdecode
from typing import Union, Dict, List

bk_fastresume_path = '~/.config/deluge/archive/torrents.fastresume'
'之前备份的文件 fastresume'
fastresume_path = '~/.config/deluge/state/torrents.fastresume'
'现在的 fastresume'

type_resume = Dict[bytes, Dict[bytes, Union[int, bytes, List[int], List[List[int]], List[List[bytes]], List[Dict[bytes, Union[int, bytes]]]]]]
resume_bk_data: type_resume = {b_id: bdecode(be_data) for b_id, be_data in bdecode(bk_fastresume_path).items()}
resume_data: type_resume = {b_id: bdecode(be_data) for b_id, be_data in bdecode(fastresume_path).items()}

for _id, data in resume_data.items():
    if _id in resume_bk_data:
        bk_data = resume_bk_data[_id]
        for key, val in data.items():
            if key in bk_data:
                bk_val = bk_data[key]
                if key in (b'active_time', b'seeding_time', b'total_downloaded', b'total_uploaded', b'finished_time'):
                    data[key] += bk_val
                elif key in (b'added_time', b'completed_time', b'last_seen_complete'):
                    data[key] = bk_val
                elif key in (b'last_download', b'last_upload'):
                    data[key] = max(val, bk_val)

with open(fastresume_path, 'wb') as _file:
    _file.write(bencode({_id: bencode(data) for _id, data in resume_data.items()}))

# 一次remux多个电影原盘
# 字幕只选择中文和英文字幕，如果都没有，则选择一条其他语言的字幕
# 音轨中文和英文各选取一条，如果都没有，则选择一条其他语言的音轨
# 音轨选择顺序：truehd>dts_hd_ma>dts>lpcm>eac3>ac3>aac
# 将无损音轨压缩成flac(TrueHD除外)
# 自动设置文件名
# 自动设置封面
# 原盘中其余时常大于30s的m2ts将作为特典放入SPs文件夹

import json
import os
import re
import subprocess
import xml.etree.ElementTree as et
from struct import unpack

import pycountry

movie_folder = r'D:\tmp'  # 电影原盘所在的目录
output_folder = r'K:\tmp'  # 输出remux的目录
flac_path = 'flac.exe'  # flac所在的目录
flac_threads = 16  # flac压缩线程数


class Chapter:
    formats: dict[int, str] = {1: '>B', 2: '>H', 4: '>I', 8: '>Q'}

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.in_out_time: list[tuple[str, int, int]] = []
        self.mark_info: dict[int, list[int]] = {}
        self.pid_to_lang = {}

        with open(self.file_path, 'rb') as self.mpls_file:
            self.mpls_file.seek(8)
            playlist_start_address = self._unpack_byte(4)
            playlist_mark_start_address = self._unpack_byte(4)

            self.mpls_file.seek(playlist_start_address)
            self.mpls_file.read(6)
            nb_play_items = self._unpack_byte(2)
            self.mpls_file.read(2)
            for _ in range(nb_play_items):
                pos = self.mpls_file.tell()
                length = self._unpack_byte(2)
                if length != 0:
                    clip_information_filename = self.mpls_file.read(5).decode()
                    self.mpls_file.read(7)
                    in_time = self._unpack_byte(4)
                    out_time = self._unpack_byte(4)
                    self.in_out_time.append((clip_information_filename, in_time, out_time))
                self.mpls_file.seek(pos + length + 2)

            self.mpls_file.seek(playlist_mark_start_address)
            self.mpls_file.read(4)
            nb_playlist_marks = self._unpack_byte(2)
            for _ in range(nb_playlist_marks):
                self.mpls_file.read(2)
                ref_to_play_item_id = self._unpack_byte(2)
                mark_timestamp = self._unpack_byte(4)
                self.mpls_file.read(6)
                if ref_to_play_item_id in self.mark_info:
                    self.mark_info[ref_to_play_item_id].append(mark_timestamp)
                else:
                    self.mark_info[ref_to_play_item_id] = [mark_timestamp]

    def _unpack_byte(self, n: int):
        return unpack(self.formats[n], self.mpls_file.read(n))[0]

    def get_total_time(self):
        return sum(map(lambda x: (x[2] - x[1]) / 45000, self.in_out_time))

    def get_total_time_no_repeat(self):
        return sum({x[0]: (x[2] - x[1]) / 45000 for x in self.in_out_time}.values())

    def get_pid_to_language(self):
        with open(self.file_path, 'rb') as self.mpls_file:
            self.mpls_file.seek(8)
            playlist_start_address = self._unpack_byte(4)
            self.mpls_file.seek(playlist_start_address)
            self.mpls_file.read(6)
            nb_of_play_items = self._unpack_byte(2)
            self.mpls_file.read(2)
            for _ in range(nb_of_play_items):
                self.mpls_file.read(12)
                is_multi_angle = (self._unpack_byte(1) >> 4) % 2
                self.mpls_file.read(21)
                if is_multi_angle:
                    nb_of_angles = self._unpack_byte(1)
                    self.mpls_file.read(1)
                    for _ in range(nb_of_angles - 1):
                        self.mpls_file.read(9)
                self.mpls_file.read(4)
                nb = []
                for _ in range(8):
                    nb.append(self._unpack_byte(1))
                self.mpls_file.read(4)
                for _ in range(sum(nb)):
                    stream_entry_length = self._unpack_byte(1)
                    stream_type = self._unpack_byte(1)
                    if stream_type == 1:
                        stream_pid = self._unpack_byte(2)
                        self.mpls_file.read(stream_entry_length - 3)
                    elif stream_type == 2:
                        self.mpls_file.read(2)
                        stream_pid = self._unpack_byte(2)
                        self.mpls_file.read(stream_entry_length - 5)
                    elif stream_type == 3 or stream_type == 4:
                        self.mpls_file.read(1)
                        stream_pid = self._unpack_byte(2)
                        self.mpls_file.read(stream_entry_length - 4)
                    stream_attributes_length = self._unpack_byte(1)
                    stream_coding_type = self._unpack_byte(1)
                    if stream_coding_type in (1, 2, 27, 36, 234):
                        self.pid_to_lang[stream_pid] = 'und'
                        self.mpls_file.read(stream_attributes_length - 1)
                    elif stream_coding_type in (3, 4, 128, 129, 130, 131, 132, 133, 134, 146, 161, 162):
                        self.mpls_file.read(1)
                        self.pid_to_lang[stream_pid] = self.mpls_file.read(3).decode()
                        self.mpls_file.read(stream_attributes_length - 5)
                    elif stream_coding_type in (144, 145):
                        self.pid_to_lang[stream_pid] = self.mpls_file.read(3).decode()
                        self.mpls_file.read(stream_attributes_length - 4)
                break



def extract_lossless(mkv_file: str, dolby_truehd_tracks: list[int]) -> tuple[int, dict[int, str]]:
    process = subprocess.Popen(f'mkvinfo "{mkv_file}" --ui-language en',
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding='utf-8', errors='ignore')
    stdout, stderr = process.communicate()

    track_info = {}
    track_count = 0
    track_suffix_info = {}
    for line in stdout.splitlines():
        if line.startswith('|  + Track number: '):
            track_id = int(re.findall(r'\d+', line.removeprefix('|  + Track number: '))[0]) - 1
            track_count = max(track_count, track_id)
        if line.startswith('|  + Codec ID: '):
            codec_id = line.removeprefix('|  + Codec ID: ').strip()
            code_id_to_stream_type = {'A_DTS': 'DTS', 'A_PCM/INT/LIT': 'LPCM', 'A_PCM/INT/BIG': 'LPCM', 'A_TRUEHD': 'TRUEHD', 'A_MLP': 'TRUEHD'}
            stream_type = code_id_to_stream_type.get(codec_id)
        if line.startswith('|  + Language (IETF BCP 47): '):
            bcp_47_code = line.removeprefix('|  + Language (IETF BCP 47): ').strip()
            language = pycountry.languages.get(alpha_2=bcp_47_code.split('-')[0])
            if language is None:
                language = pycountry.languages.get(alpha_3=bcp_47_code.split('-')[0])
            if language:
                lang = getattr(language, "bibliographic", getattr(language, "alpha_3", None))
            else:
                lang = 'und'
            if stream_type in ('LPCM', 'DTS', 'TRUEHD'):
                if track_id not in dolby_truehd_tracks:
                    track_info[track_id] = lang
                    if stream_type == 'LPCM':
                        track_suffix_info[track_id] = 'wav'
                    elif stream_type == 'DTS':
                        track_suffix_info[track_id] = 'dts'
                    else:
                        track_suffix_info[track_id] = 'thd'

    if track_info:
        extract_info = []
        for track_id, lang in track_info.items():
            extract_info.append(f'{track_id}:"{mkv_file.removesuffix(".mkv")}.track{track_id}.{track_suffix_info[track_id]}"')
        subprocess.Popen(f'mkvextract "{mkv_file}" tracks {" ".join(extract_info)}').wait()

    return track_count, track_info


def generate_remux_cmd(track_count, track_info, flac_files, output_file, mkv_file):
    tracker_order = []
    audio_tracks = []
    pcm_track_count = 0
    language_options = []
    for _ in range(track_count + 1):
        if _ in track_info:
            pcm_track_count += 1
            tracker_order.append(f'{pcm_track_count}:0')
            audio_tracks.append(str(_))
            language_options.append(f'--language 0:{track_info[_]} "{flac_files[pcm_track_count - 1]}"')
        else:
            tracker_order.append(f'0:{_}')
    tracker_order = ','.join(tracker_order)
    audio_tracks = '!' + ','.join(audio_tracks)
    language_options = ' '.join(language_options)
    return (f'mkvmerge -o "{output_file}" --track-order {tracker_order} '
            f'-a {audio_tracks} "{mkv_file}" {language_options}')


def get_index_to_m2ts_and_offset(chapter: Chapter) -> tuple[dict[int, str], dict[int, float]]:
    j = 1
    rows = sum(map(len, chapter.mark_info.values()))
    index_to_m2ts = {}
    index_to_offset = {}
    offset = 0
    for ref_to_play_item_id, mark_timestamps in chapter.mark_info.items():
        for mark_timestamp in mark_timestamps:
            index_to_m2ts[j] = chapter.in_out_time[ref_to_play_item_id][0] + '.m2ts'
            off = offset + (mark_timestamp -
                            chapter.in_out_time[ref_to_play_item_id][1]) / 45000
            index_to_offset[j] = off
            j += 1
        offset += (chapter.in_out_time[ref_to_play_item_id][2] -
                   chapter.in_out_time[ref_to_play_item_id][1]) / 45000
        index_to_offset[rows + j] = offset
    return index_to_m2ts, index_to_offset


class M2TS:
    def __init__(self, filename: str):
        self.filename = filename
        self.frame_size = 192

    def get_duration(self) -> int:
        with open(self.filename, "rb") as self.m2ts_file:
            try:
                buffer_size = 256 * 1024
                buffer_size -= buffer_size % self.frame_size
                cur_pos = 0
                first_pcr_val = -1
                while cur_pos < buffer_size:
                    self.m2ts_file.read(7)
                    first_pcr_val = self.get_pcr_val()
                    self.m2ts_file.read(182)
                    if first_pcr_val != -1:
                        break

                buffer_size = 256 * 1024
                buffer_size -= buffer_size % self.frame_size
                last_pcr_val = self.get_last_pcr_val(buffer_size)
                buffer_size *= 4

                while last_pcr_val == -1 and buffer_size <= 1024 * 1024:
                    last_pcr_val = self.get_last_pcr_val(buffer_size)
                    buffer_size *= 4

                return 0 if last_pcr_val == -1 else last_pcr_val - first_pcr_val
            except:
                return 0

    def get_last_pcr_val(self, buffer_size) -> int:
        last_pcr_val = -1
        file_size = os.path.getsize(self.filename)
        cur_pos = max(file_size - file_size % self.frame_size - buffer_size, 0)
        buffer_end = cur_pos + buffer_size
        while cur_pos <= buffer_end - self.frame_size:
            self.m2ts_file.seek(cur_pos + 7)
            _last_pcr_val = self.get_pcr_val()
            if _last_pcr_val != -1:
                last_pcr_val = _last_pcr_val
            cur_pos += self.frame_size
        return last_pcr_val

    def unpack_bytes(self, n: int) -> int:
        formats: dict[int, str] = {1: '>B', 2: '>H', 4: '>I', 8: '>Q'}
        return unpack(formats[n], self.m2ts_file.read(n))[0]

    def get_pcr_val(self) -> int:
        af_exists = (self.unpack_bytes(1) >> 5) % 2
        adaptive_field_length = self.unpack_bytes(1)
        pcr_exist = (self.unpack_bytes(1) >> 4) % 2
        if af_exists and adaptive_field_length and pcr_exist:
            tmp = []
            for _ in range(4):
                tmp.append(self.unpack_bytes(1))
            pcr = tmp[3] + (tmp[2] << 8) + (tmp[1] << 16) + (tmp[0] << 24)
            pcr_lo = self.unpack_bytes(1) >> 7
            pcr_val = (pcr << 1) + pcr_lo
            return pcr_val
        return -1


for file in os.listdir(movie_folder):
    if os.path.isdir(os.path.join(movie_folder, file)):
        for root, dirs, files in os.walk(os.path.join(movie_folder, file)):
            if 'BDMV' in dirs and 'PLAYLIST' in os.listdir(os.path.join(root, 'BDMV')):
                mpls_folder = os.path.join(root, 'BDMV', 'PLAYLIST')
                max_indicator = 0
                selected_mpls = ''
                for filename in os.listdir(mpls_folder):
                    if filename.endswith('.mpls'):
                        mpls_file = os.path.join(mpls_folder, filename)
                        chapter = Chapter(mpls_file)
                        total_size = 0
                        stream_files = set()
                        for in_out_time in chapter.in_out_time:
                            if in_out_time[0] not in stream_files:
                                total_size += os.path.getsize(os.path.join(root, 'BDMV', 'STREAM', f'{in_out_time[0]}.m2ts'))
                                # 计算播放列表文件总体积(重复文件只计算一次)
                            stream_files.add(in_out_time[0])
                        indicator = chapter.get_total_time_no_repeat() * (1 + sum(map(len, chapter.mark_info.values())) / 5) * os.path.getsize(mpls_file) * total_size
                        # 有些播放列表轨道信息不全，所以这里乘了mpls的体积，选取mpls体积最大的
                        if indicator >= max_indicator:
                            max_indicator = indicator
                            selected_mpls = mpls_file
                sub_folder = root.removeprefix(os.path.join(movie_folder, file))
                chapter = Chapter(selected_mpls)
                chapter.get_pid_to_language()
                m2ts_file = os.path.join(os.path.join(mpls_folder[:-9], 'STREAM'), Chapter(selected_mpls).in_out_time[0][0] + '.m2ts')
                cmd = f'ffprobe -v error -show_streams -show_format -of json "{m2ts_file}" >info.json 2>&1'
                subprocess.Popen(cmd, shell=True).wait()
                with open('info.json', 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                audio_type_weight = {'': -1, 'aac': 1, 'ac3': 2, 'eac3': 3, 'lpcm': 4, 'dts': 5, 'dts_hd_ma': 6, 'truehd': 7}
                selected_eng_audio_track = ['', '']
                selected_zho_audio_track = ['', '']
                copy_sub_track = []
                for stream_info in data['streams']:
                    if stream_info['codec_type'] == 'audio':
                        codec_name = stream_info['codec_name']
                        if codec_name == 'dts' and stream_info.get('profile') == 'DTS-HD MA':
                            codec_name = 'dts_hd_ma'
                        lang = chapter.pid_to_lang.get(int(stream_info['id'], 16), 'und')
                        if lang == 'eng':
                            if not selected_eng_audio_track[1] or audio_type_weight[codec_name] > audio_type_weight[
                                selected_eng_audio_track[1]]:
                                selected_eng_audio_track = [str(stream_info['index']), codec_name]
                        elif lang == 'zho':
                            if not selected_zho_audio_track[1] or audio_type_weight[codec_name] > audio_type_weight[
                                selected_zho_audio_track[1]]:
                                selected_zho_audio_track = [str(stream_info['index']), codec_name]
                    elif stream_info['codec_type'] == 'subtitle':
                        lang = chapter.pid_to_lang.get(int(stream_info['id'], 16), 'und')
                        if lang in ['eng', 'zho']:
                            copy_sub_track.append(str(stream_info['index']))
                if not copy_sub_track:
                    for stream_info in data['streams']:
                        if stream_info['codec_type'] == 'subtitle':
                            copy_sub_track.append(str(stream_info['index']))
                            break
                if not selected_zho_audio_track[0] and not selected_eng_audio_track[0]:
                    copy_audio_track = []
                    for stream_info in data['streams']:
                        if stream_info['codec_type'] == 'audio':
                            copy_audio_track.append(str(stream_info['index']))
                            break
                else:
                    if selected_eng_audio_track[0] and selected_zho_audio_track[0]:
                        copy_audio_track = [selected_eng_audio_track[0] , selected_zho_audio_track[0]]
                    elif not selected_eng_audio_track[0]:
                        copy_audio_track = [selected_zho_audio_track[0]]
                    else:
                        copy_audio_track = [selected_eng_audio_track[0]]
                    first_audio_index = 1
                    for stream_info in data['streams']:
                        if stream_info['codec_type'] == 'audio':
                            first_audio_index = stream_info['index']
                            break
                    if str(first_audio_index) not in (selected_zho_audio_track[0], selected_eng_audio_track[0]):
                        copy_audio_track.append(str(first_audio_index))

                meta_folder = os.path.join(os.path.join(mpls_folder[:-9], 'META', 'DL'))
                cover = ''
                cover_size = 0
                if not os.path.exists(meta_folder):
                    output_name = os.path.split(mpls_folder[:-14])[-1]
                else:
                    for filename in os.listdir(meta_folder):
                        # 获取附件Cover
                        if filename.endswith('.jpg') or filename.endswith('.JPG') or filename.endswith('.JPEG') or filename.endswith('.jpeg') or filename.endswith('.png') or filename.endswith('.PNG'):
                            if os.path.getsize(os.path.join(meta_folder, filename)) > cover_size:
                                cover = os.path.join(meta_folder, filename)
                                cover_size = os.path.getsize(os.path.join(meta_folder, filename))
                    # 获取输出文件名
                    output_name = ''
                    for filename in os.listdir(meta_folder):
                        if filename == 'bdmt_eng.xml':
                            tree = et.parse(os.path.join(meta_folder, filename))
                            root = tree.getroot()
                            ns = {'di': 'urn:BDA:bdmv;discinfo'}
                            output_name = root.find('.//di:name', ns).text
                            break
                    if not output_name:
                        for filename in os.listdir(meta_folder):
                            if filename == 'bdmt_zho.xml':
                                tree = et.parse(os.path.join(meta_folder, filename))
                                root = tree.getroot()
                                ns = {'di': 'urn:BDA:bdmv;discinfo'}
                                output_name = root.find('.//di:name', ns).text
                                break
                    if not output_name:
                        for filename in os.listdir(meta_folder):
                            tree = et.parse(os.path.join(meta_folder, filename))
                            root = tree.getroot()
                            ns = {'di': 'urn:BDA:bdmv;discinfo'}
                            output_name = root.find('.//di:name', ns).text
                            break
                    if not output_name:
                        output_name = os.path.split(mpls_folder[:-14])[-1]
                char_map = {
                    '?': '？',
                    '*': '★',
                    '<': '《',
                    '>': '》',
                    ':': '：',
                    '"': "'",
                    '/': '／',
                    '\\': '／',
                    '|': '￨'
                }
                output_name = ''.join(char_map.get(char) or char for char in output_name)

                dst_folder = os.path.join(output_folder, os.path.split(mpls_folder[:-14])[-1])
                if sub_folder:
                    dst_folder = os.path.join(dst_folder, sub_folder)
                output_file = os.path.join(dst_folder, output_name) + '.mkv'

                remux_cmd = f'mkvmerge -o "{output_file}" {("-a " + ",".join(copy_audio_track)) if copy_audio_track else ""} {("-s " + ",".join(copy_sub_track)) if copy_sub_track else ""} {(" --attachment-name Cover.jpg" + " --attach-file " + "\"" + cover + "\"") if cover else "" } "{selected_mpls}"'
                print(f'混流命令: {remux_cmd}')
                subprocess.Popen(remux_cmd).wait()
                dolby_truehd_tracks = []
                track_bits = {}
                if os.path.exists(output_file):
                    subprocess.Popen(f'ffprobe -v error -show_streams -show_format -of json "{output_file}" >info.json 2>&1', shell=True).wait()
                    with open('info.json', 'r', encoding='utf-8') as fp:
                        data = json.load(fp)
                    for stream in data['streams']:
                        if stream['codec_name'] == 'truehd' and stream.get('profile') == 'Dolby TrueHD + Dolby Atmos':
                            dolby_truehd_tracks.append(stream['index'])
                        if stream['codec_name'] in ('truehd', 'dts'):
                            track_bits[stream['index']] = int(stream.get('bits_per_raw_sample') or 24)
                track_count, track_info = extract_lossless(output_file, dolby_truehd_tracks)
                if track_info:
                    for file1 in os.listdir(dst_folder):
                        file1_path = os.path.join(dst_folder, file1)
                        if file1_path != output_file:
                            if file1_path.endswith('.wav'):
                                n = len(os.listdir(dst_folder))
                                subprocess.Popen(f'"{flac_path}" -8 -j {flac_threads} "{file1_path}"').wait()
                                if len(os.listdir(dst_folder)) > n:
                                    os.remove(file1_path)
                            else:
                                track_id = int(os.path.split(file1_path)[-1].split('.')[-2].removeprefix('track'))
                                bits = track_bits.get(track_id, 24)
                                wav_file = os.path.splitext(file1_path)[0] + '.wav'
                                n = len(os.listdir(dst_folder))
                                subprocess.Popen(f'ffmpeg -i "{file1_path}"  -c:a pcm_s{bits}le -f w64 "{wav_file}"').wait()
                                flac_file = os.path.splitext(file1_path)[0] + '.flac'
                                subprocess.Popen(f'flac -8 -j {flac_threads} "{wav_file}" -o "{flac_file}"').wait()
                                if os.path.getsize(flac_file) > os.path.getsize(file1_path):
                                    os.remove(flac_file)
                                os.remove(file1_path)
                                os.remove(wav_file)
                    flac_files = []
                    for file1 in os.listdir(dst_folder):
                        file1_path = os.path.join(dst_folder, file1)
                        if file1_path.endswith('.flac'):
                            flac_files.append(file1_path)
                    if not flac_files:
                        for file1 in os.listdir(dst_folder):
                            file1_path = os.path.join(dst_folder, file1)
                            if file1_path != output_file:
                                if file1_path.endswith('.wav'):
                                    n = len(os.listdir(dst_folder))
                                    subprocess.Popen(f'ffmpeg -i "{file1_path}" -c:a flac "{file1_path.removesuffix(".wav") + ".flac"}"').wait()
                                    if len(os.listdir(dst_folder)) > n:
                                        os.remove(file1_path)
                        for file1 in os.listdir(dst_folder):
                            file1_path = os.path.join(dst_folder, file1)
                            if file1_path.endswith('.flac'):
                                flac_files.append(file1_path)
                    if flac_files:
                        output_file1 = os.path.join(dst_folder, os.path.splitext(output_file)[0] + '(1).mkv')
                        remux_cmd = generate_remux_cmd(track_count, track_info, flac_files, output_file1, output_file)
                        subprocess.Popen(remux_cmd).wait()
                        if os.path.getsize(output_file1) > os.path.getsize(output_file):
                            os.remove(output_file1)
                        else:
                            os.remove(output_file)
                            os.rename(output_file1, output_file)
                        for flac_file in flac_files:
                            os.remove(flac_file)

                sps_folder = os.path.join(dst_folder, 'SPs')
                os.makedirs(sps_folder, exist_ok=True)
                index_to_m2ts, index_to_offset = get_index_to_m2ts_and_offset(Chapter(selected_mpls))
                parsed_m2ts_files = set(index_to_m2ts.values())
                sp_index = 0
                for mpls_file in os.listdir(os.path.dirname(mpls_folder)):
                    if not mpls_file.endswith('.mpls'):
                        continue
                    mpls_file_path = os.path.join(os.path.dirname(mpls_folder), mpls_file)
                    if mpls_file_path != mpls_folder:
                        index_to_m2ts, index_to_offset = get_index_to_m2ts_and_offset(Chapter(mpls_file_path))
                        if not (parsed_m2ts_files & set(index_to_m2ts.values())):
                            if len(index_to_m2ts) > 1:
                                sp_index += 1
                                subprocess.Popen(f'mkvmerge -o "{sps_folder}{os.sep}'
                                                 f'SP{sp_index}.mkv" "{mpls_file_path}"').wait()
                                parsed_m2ts_files |= set(index_to_m2ts.values())
                stream_folder = os.path.join(os.path.dirname(mpls_folder).removesuffix('PLAYLIST'), 'STREAM')
                for stream_file in os.listdir(stream_folder):
                    if stream_file not in parsed_m2ts_files and stream_file.endswith('.m2ts'):
                        if M2TS(os.path.join(stream_folder, stream_file)).get_duration() > 30 * 90000:
                            subprocess.Popen(f'mkvmerge -o "{sps_folder}{os.sep}'
                                             f'{stream_file[:-5]}.mkv" '
                                             f'"{os.path.join(stream_folder, stream_file)}"').wait()
                for sp in os.listdir(sps_folder):
                    mkv_file = os.path.join(sps_folder, sp)
                    dolby_truehd_tracks = []
                    track_bits = {}
                    subprocess.Popen(f'ffprobe -v error -show_streams -show_format -of json "{mkv_file}" >info.json 2>&1', shell=True).wait()
                    with open('info.json', 'r', encoding='utf-8') as fp:
                        data = json.load(fp)
                    for stream in data['streams']:
                        if stream['codec_name'] == 'truehd' and stream.get('profile') == 'Dolby TrueHD + Dolby Atmos':
                            dolby_truehd_tracks.append(stream['index'])
                        if stream['codec_name'] in ('truehd', 'dts'):
                            track_bits[stream['index']] = int(stream.get('bits_per_raw_sample') or 24)
                    track_count, track_info = extract_lossless(mkv_file, dolby_truehd_tracks)
                    if track_info:
                        for file1 in os.listdir(sps_folder):
                            file1_path = os.path.join(sps_folder, file1)
                            if file1_path != mkv_file and file1_path.startswith(mkv_file.removesuffix('.mkv')):
                                if file1_path.endswith('.wav'):
                                    n = len(os.listdir(sps_folder))
                                    subprocess.Popen(f'"{flac_path}" -8 -j {flac_threads} "{file1_path}"').wait()
                                    if len(os.listdir(sps_folder)) > n:
                                        os.remove(file1_path)
                                else:
                                    track_id = int(file1.split('.')[-2].removeprefix('track'))
                                    bits = track_bits.get(track_id, 24)
                                    # ffmpeg直接转flac太慢，先将dts转成wav，再将wav转成flac
                                    wav_file = os.path.splitext(file1_path)[0] + '.wav'
                                    n = len(os.listdir(sps_folder))
                                    subprocess.Popen(f'ffmpeg -i "{file1_path}"  -c:a pcm_s{bits}le -f w64 "{wav_file}"').wait()
                                    flac_file = os.path.splitext(file1_path)[0] + '.flac'
                                    subprocess.Popen(f'flac -8 -j {flac_threads} "{wav_file}" -o "{flac_file}"').wait()
                                    if os.path.getsize(flac_file) > os.path.getsize(file1_path):
                                        # 如果转换出来的flac体积比dts体积大则舍弃，这种情况很常见
                                        os.remove(flac_file)
                                    os.remove(file1_path)
                                    os.remove(wav_file)
                        flac_files = []
                        for file1 in os.listdir(sps_folder):
                            if file1.endswith('.flac'):
                                flac_files.append(os.path.join(sps_folder, file1))
                        if not flac_files:
                            for file1 in os.listdir(sps_folder):
                                file1_path = os.path.join(sps_folder, file1)
                                if file1_path != mkv_file and file1_path.startswith(mkv_file.removesuffix('.mkv')):
                                    if file1_path.endswith('.wav'):
                                        n = len(os.listdir(sps_folder))
                                        subprocess.Popen(f'ffmpeg -i "{file1_path}" -c:a flac "{file1_path.removesuffix(".wav") + ".flac"}"').wait()
                                        if len(os.listdir(sps_folder)) > n:
                                            os.remove(file1_path)
                            for file1 in os.listdir(sps_folder):
                                if file1.endswith('.flac'):
                                    flac_files.append(os.path.join(sps_folder, file1))
                        if flac_files:
                            output_file = os.path.join(sps_folder, os.path.splitext(sp)[0] + '(1).mkv')
                            remux_cmd = generate_remux_cmd(track_count, track_info, flac_files, output_file, mkv_file)
                            print(f'混流命令: {remux_cmd}')
                            subprocess.Popen(remux_cmd).wait()
                            if os.path.getsize(output_file) > os.path.getsize(mkv_file):
                                os.remove(output_file)
                            else:
                                os.remove(mkv_file)
                                os.rename(output_file, mkv_file)
                            for flac_file in flac_files:
                                os.remove(flac_file)

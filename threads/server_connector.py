import os
import threading
import pickle
from time import sleep
from datetime import datetime, timedelta

import paramiko
from paramiko.ssh_exception import SSHException
from moviepy.video.io.ffmpeg_tools import ffmpeg_extract_subclip

from utils.redis_client import redis_client, redis_client_pickle
from config import Config
from utils.db import DBConnect
from logs.logger import Logger


class HomeServerConnector(threading.Thread):
    def __init__(self, url: str, username: str, password: str, destination_path: str):
        super().__init__()

        self.url = url
        self.username = username
        self.password = password
        self.destination_path = destination_path
        self.logger = Logger('HomeServerConnector')

    def check_destination_path(self, sftp_client):
        try:
            sftp_client.stat(self.destination_path)
        except FileNotFoundError:
            self.logger.warning('Destination path doesnt exist!')
            sftp_client.mkdir(self.destination_path)

    def upload_regular_file(self, sftp):
        # получение имени файла из очереди в redis сервере
        filename = redis_client.lrange('ready_to_send', 0, 0)[0]
        filepath = os.path.join(Config.MEDIA_PATH, filename)
        try:
            # отправка файла на удаленный сервер
            self.logger.info(f'start upload {filename}')
            sftp.put(filepath, os.path.join(self.destination_path, filename))

            start_time = datetime.strptime(filename[:19], Config.DATETIME_FORMAT)
            finish_time = start_time + timedelta(seconds=int(redis_client.get(filename)) // Config.FPS)

            # подключение к базе данных
            with DBConnect(Config.DATABASE_URL, Config.CAR_ID) as conn:
                # запись данных о видео в удаленную бд
                conn.add_record(filename=filename,
                                start_time=start_time,
                                finish_time=finish_time)
        except FileNotFoundError:
            redis_client.delete(filename)
            redis_client.lpop('ready_to_send')
        except OSError as e:
            self.logger.exception(f'Some error occurred, {filename} not uploaded: {e}')
        except EOFError as e:
            self.logger.exception(f'SSH connection error: {e}')
        else:
            # удаление выгруженного файла из памяти и очереди в redis
            os.remove(filepath)
            redis_client.lpop('ready_to_send')
            self.logger.info(f'{filepath} upload complete')

    def upload_requested_files(self, sftp):
        # получение имени файлов из очереди в redis сервере
        request = pickle.loads(redis_client_pickle.lrange('ready_requested_videos', 0, 0)[0])
        pk = request['request_pk']
        files = request['files']
        duration = request['duration']

        for filename in files:
            filepath = os.path.join(Config.MEDIA_PATH, 'temp', filename)
            try:
                # отправка файла на удаленный сервер
                self.logger.info(f'start upload {filename}')
                sftp.put(filepath, os.path.join(self.destination_path, filename))
                start_time = datetime.strptime(filename[:19], Config.DATETIME_FORMAT)
                finish_time = start_time + duration

                # подключение к базе данных
                with DBConnect(Config.DATABASE_URL, Config.CAR_ID) as conn:
                    # запись данных о видео в удаленную бд
                    conn.add_record(filename=filename,
                                    start_time=start_time,
                                    finish_time=finish_time,
                                    pk=pk)
            except FileNotFoundError:
                redis_client_pickle.lpop('ready_requested_videos')
            except OSError as e:
                self.logger.exception(f'Some error occurred, {filename} not uploaded: {e}')
            except EOFError as e:
                self.logger.exception(f'SSH connection error: {e}')
            else:
                # удаление выгруженного файла из памяти и очереди в redis
                os.remove(filepath)
                redis_client_pickle.lpop('ready_requested_videos')
                self.logger.info(f'{filepath} upload complete')

        with DBConnect(Config.DATABASE_URL, Config.CAR_ID) as conn:
            # запись данных о видео в удаленную бд
            status = True if files else False

            conn.set_request_status(pk=pk, status=status)

    def upload_files(self):
        """
        Выгрузка файлов на сервер с помощью SFTP
        Запись данных о файлах в удаленную базу данных
        """
        # создание SSH подключения

        if redis_client.llen('ready_to_send') or redis_client.llen('ready_requested_videos'):
            with paramiko.SSHClient() as client:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(hostname=self.url,
                               username=self.username,
                               password=self.password,
                               auth_timeout=30,
                               timeout=30,
                               banner_timeout=30)

                # создание sftp поверх ssh
                with client.open_sftp() as sftp:
                    sftp.get_channel().settimeout(30)

                    self.check_destination_path(sftp)

                    for _ in range(redis_client.llen('ready_to_send')):
                        self.upload_regular_file(sftp)
                    for _ in range(redis_client.llen('ready_requested_videos')):
                        self.upload_requested_files(sftp)

    def send_coordinates(self):
        """Отправка координат в удаленную базу данных"""
        # подключение к базе данных
        with DBConnect(Config.DATABASE_URL, Config.CAR_ID) as conn:
            for _ in range(redis_client.llen('coordinates')):
                try:
                    # получение и десериализация координат из очереди redis
                    coordinates = pickle.loads(redis_client_pickle.lrange('coordinates', 0, 0)[0])
                    # отправка координат в бд
                    conn.add_coordinates(coordinates)
                except Exception as e:
                    self.logger.exception(f'Some error occurred, coordinates not uploaded: {e}')
                else:
                    # удаление координат из очереди redis
                    redis_client_pickle.lpop('coordinates')
            else:
                self.logger.info(f'coordinates upload complete')

    def check_video_requests(self):
        with DBConnect(Config.DATABASE_URL, Config.CAR_ID) as conn:
            # получение запросов на видеозаписи
            requests = conn.get_record_requests()
            [pickle.dumps(request) for request in requests]
            redis_client_pickle.lpush('requests', requests)

    def make_clip(self, filename, file_start, start_time, finish_time):
        file_full_path = os.path.join(Config.MEDIA_PATH, filename)

        # Получение смещений для вырезки части клипа
        start_offset = start_time - file_start
        finish_offset = finish_time - file_start

        # Формирование имени выходного файла
        out_filename = f'{datetime.strftime(start_time, Config.DATETIME_FORMAT)}_' \
                       f'{filename.split("_")[-1]}'
        out_full_path = os.path.join(Config.MEDIA_PATH, "temp", out_filename)

        # Формирования выходного файла
        ffmpeg_extract_subclip(
            file_full_path,
            start_offset.total_seconds(),
            finish_offset.total_seconds(),
            targetname=out_full_path)

        return out_filename

    def make_clips_by_request(self):
        # Получение запросов
        self.check_video_requests()
        # Получение списка записанных файлов
        filenames = [file for file in os.listdir(Config.MEDIA_PATH) if 'BodyCam' not in file]
        filenames.remove('temp')

        for _ in redis_client_pickle.llen('requests'):
            request = pickle.loads(redis_client_pickle.lpop('requests'))

            request_files = []
            start_time = request['start_time'].replace(tzinfo=None)
            finish_time = request['finish_time'].replace(tzinfo=None)

            contains_full = []  # Список файлов которые содержат полное запрошенное видео
            contains_start = []  # Список файлов содержат начало запрошенных видео
            contains_finish = []  # Список файлов содержат конец запрошенных видео
            for filename in filenames:

                # Парсинг имени файла
                file_start = datetime.strptime(filename[:19], Config.DATETIME_FORMAT)
                file_finish = file_start + timedelta(int(redis_client.get(filename)) // Config.FPS)

                # Проверка видео, подходит ли оно под запрос и наполнение списков
                if file_start <= start_time <= file_finish and file_start <= finish_time <= file_finish:
                    contains_full.append(filename)
                elif file_start <= start_time <= file_finish:
                    contains_start.append(filename)
                elif file_start <= finish_time <= file_finish:
                    contains_finish.append(filename)

                # Формирование клипов
                for file in contains_full:
                    out_filename = self.make_clip(file,
                                                  file_start,
                                                  start_time=start_time,
                                                  finish_time=finish_time)

                    request_files.append(out_filename)
                for file in contains_start:
                    out_filename = self.make_clip(file,
                                                  file_start,
                                                  start_time=start_time,
                                                  finish_time=file_finish)
                    request_files.append(out_filename)
                for file in contains_finish:
                    out_filename = self.make_clip(file,
                                                  file_start,
                                                  start_time=file_start,
                                                  finish_time=finish_time)
                    request_files.append(out_filename)

            result_dict = {
                'request_pk': request['id'],
                'files': request_files,
                'duration': finish_time - start_time,
            }

            redis_client_pickle.lpush('ready_requested_videos', pickle.dumps(result_dict))

    def run(self):
        """
        Запуск бесконечного цикла.
        Попытка выгрузки файлов и координат в каждой итерации.
        В случае неудачи следущая попытка осуществляется через (хронометраж видео / 6).
        """

        while True:
            try:
                self.send_coordinates()
                self.make_clips_by_request()
                self.upload_files()
            except AttributeError as e:
                self.logger.info(f"no connection, will try later {e}")
            except SSHException as e:
                self.logger.info(f"no connection, {e}")
            except Exception as e:
                self.logger.exception(f"Unexpected error: {e}")
            sleep(Config.VIDEO_DURATION.total_seconds() // 6)

from __future__ import absolute_import, unicode_literals

import hashlib
import json
import logging
import os
import socket
import subprocess
from datetime import datetime, timedelta
from ftplib import FTP, error_perm, error_reply
from time import sleep, time

import cronex
import dateutil.parser
import pandas
import psycopg2
import pytz
import requests
from celery import shared_task
from celery.utils.log import get_task_logger
from django.core.cache import cache
from django.db import connection

from ..tempestas_api import settings
from .decoders.flash import read_data as read_data_flash
from .decoders.hobo import read_file as read_file_hobo
from .decoders.hydro import read_file as read_file_hydrology
from .decoders.manual_data import read_file as read_file_manual_data
from .decoders.manual_data_hourly import read_file as read_file_manual_data_hourly
from .decoders.nesa import read_data as read_data_nesa
from .decoders.surface import read_file as read_file_surface
from .decoders.toa5 import read_file
from .models import DataFile
from .models import Document
from .models import NoaaDcp
from .models import Station
from .models import StationFileIngestion, StationDataFile, HourlySummaryTask, DailySummaryTask, \
    HydroMLPredictionStation, HydroMLPredictionMapping

logger = get_task_logger(__name__)
db_logger = get_task_logger('db')


def get_connection():
    return psycopg2.connect(settings.SURFACE_CONNECTION_STRING)


@shared_task
def calculate_hourly_summary(start_datetime=None, end_datetime=None, station_id_list=None):
    start_at = time()

    if not start_datetime:
        start_datetime = datetime.today() - timedelta(hours=48)

    if not end_datetime:
        end_datetime = datetime.today()

    if start_datetime.tzinfo is None:
        start_datetime = pytz.UTC.localize(start_datetime)

    if end_datetime.tzinfo is None:
        end_datetime = pytz.UTC.localize(end_datetime)

    if start_datetime > end_datetime:
        print('Error - start date is more recent than end date.')
        return

    if station_id_list is None:
        station_ids = Station.objects.filter(is_active=True).values_list('id', flat=True)
    else:
        station_ids = station_id_list

    station_ids = tuple(station_ids)

    logger.info('Hourly summary started at {}'.format(datetime.today()))
    logger.info('Hourly summary parameters: {} {} {}'.format(start_datetime, end_datetime, station_id_list))

    delete_sql = f"""
        DELETE FROM hourly_summary 
        WHERE station_id in %(station_ids)s 
          AND datetime = %(start_datetime)s
    """

    insert_sql = f"""
        INSERT INTO hourly_summary (
            datetime,
            station_id,
            variable_id,
            min_value,
            max_value,
            avg_value,
            sum_value,
            num_records,
            created_at,
            updated_at
        )
        SELECT 
            values.datetime,
            values.station_id,
            values.variable_id,
            values.min_value,
            values.max_value,
            values.avg_value,
            values.sum_value,
            values.num_records,
            now(),
            now()
        FROM
            (SELECT 
                CASE WHEN rd.datetime = rd.datetime::date THEN date_trunc('hour', rd.datetime - '1 second'::interval) ELSE date_trunc('hour', rd.datetime) END as datetime,
                station_id,
                variable_id,
                min(calc.value) AS min_value,
                max(calc.value) AS max_value,
                avg(calc.value) AS avg_value,
                sum(calc.value) AS sum_value,
                count(calc.value) AS num_records
            FROM 
                raw_data rd
                ,LATERAL (SELECT CASE WHEN rd.consisted IS NOT NULL THEN rd.consisted ELSE rd.measured END as value) AS calc
            WHERE rd.datetime >= %(start_datetime)s
              AND rd.datetime <= %(end_datetime)s
              AND (rd.manual_flag in (1,4) OR (rd.manual_flag IS NULL AND rd.quality_flag in (1,4)))
              AND NOT rd.is_daily
              AND calc.value != %(MISSING_VALUE)s
              AND station_id in %(station_ids)s
            GROUP BY 1,2,3) values
        WHERE values.datetime = %(start_datetime)s
        UNION ALL
        SELECT 
            values.datetime,
            values.station_id,
            values.variable_id,
            values.min_value,
            values.max_value,
            values.avg_value,
            values.sum_value,
            values.num_records,
            now(),
            now()
        FROM
            (SELECT date_trunc('hour', rd.datetime) as datetime,
                station_id,
                variable_id,
                min(calc.value) AS min_value,
                max(calc.value) AS max_value,
                avg(calc.value) AS avg_value,
                sum(calc.value) AS sum_value,
                count(calc.value) AS num_records
            FROM 
                raw_data rd
                ,LATERAL (SELECT CASE WHEN rd.consisted IS NOT NULL THEN rd.consisted ELSE rd.measured END as value) AS calc
            WHERE rd.datetime >= %(start_datetime)s
              AND rd.datetime <= %(end_datetime)s
              AND (rd.manual_flag in (1,4) OR (rd.manual_flag IS NULL AND rd.quality_flag in (1,4)))
              AND rd.is_daily
              AND calc.value != %(MISSING_VALUE)s
              AND station_id in %(station_ids)s
            GROUP BY 1,2,3) values
        WHERE values.datetime = %(start_datetime)s
    """

    conn = get_connection()

    with conn.cursor() as cursor:
        cursor.execute(delete_sql, {"station_ids": station_ids, "start_datetime": start_datetime})
        cursor.execute(insert_sql,
                       {"station_ids": station_ids, "start_datetime": start_datetime, "end_datetime": end_datetime,
                        "MISSING_VALUE": settings.MISSING_VALUE})
    conn.commit()
    conn.close()

    logger.info(f'Hourly summary finished at {datetime.now(pytz.UTC)}. Took {time() - start_at} seconds.')


@shared_task
def calculate_daily_summary(start_date=None, end_date=None, station_id_list=None):
    logger.info(f'DAILY SUMMARY started at {datetime.now(tz=pytz.UTC)} with parameters: '
                f'start_date={start_date} end_date={end_date} '
                f'station_id_list={station_id_list}')

    start_at = time()

    if start_date is None or end_date is None:
        start_date = datetime.now(pytz.UTC).date()
        end_date = (datetime.now(pytz.UTC) + timedelta(days=1)).date()

    if start_date > end_date:
        print('Error - start_date is more recent than end_date.')
        return

    conn = get_connection()
    with conn.cursor() as cursor:

        if station_id_list is None:
            stations = Station.objects.filter(is_active=True)
        else:
            stations = Station.objects.filter(id__in=station_id_list)

        offsets = list(set([s.utc_offset_minutes for s in stations]))
        for offset in offsets:
            station_ids = tuple(stations.filter(utc_offset_minutes=offset).values_list('id', flat=True))
            fixed_offset = pytz.FixedOffset(offset)

            datetime_start_utc = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=pytz.UTC)
            datetime_end_utc = datetime(end_date.year, end_date.month, end_date.day, 0, 0, 0, tzinfo=pytz.UTC)

            datetime_start = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0,
                                      tzinfo=fixed_offset).astimezone(pytz.UTC)
            datetime_end = datetime(end_date.year, end_date.month, end_date.day, 0, 0, 0,
                                    tzinfo=fixed_offset).astimezone(pytz.UTC)

            logger.info(f"datetime_start={datetime_start}, datetime_end={datetime_end} "
                        f"offset={offset} "
                        f"station_ids={station_ids}")

            delete_sql = """
                DELETE FROM daily_summary 
                WHERE station_id in %(station_ids)s 
                AND day >= %(datetime_start)s
                AND day < %(datetime_end)s
            """

            insert_sql = """
                INSERT INTO daily_summary (
                    "day",
                    station_id,
                    variable_id,
                    min_value,
                    max_value,
                    avg_value,
                    sum_value,
                    num_records,
                    created_at,
                    updated_at
                )
                SELECT 
                    cast((rd.datetime + interval '%(offset)s minutes') at time zone 'utc' - '1 second'::interval as DATE) as "date",
                    station_id,
                    variable_id,
                    min(calc.value) AS min_value,
                    max(calc.value) AS max_value,
                    avg(calc.value) AS avg_value,
                    sum(calc.value) AS sum_value,
                    count(calc.value) AS num_records,
                    now(),
                    now()
                FROM 
                    raw_data rd
                    ,LATERAL (SELECT CASE WHEN rd.consisted IS NOT NULL THEN rd.consisted ELSE rd.measured END as value) AS calc
                WHERE rd.datetime > %(datetime_start)s
                  AND rd.datetime <= %(datetime_end)s
                  AND calc.value != %(MISSING_VALUE)s
                  AND station_id in %(station_ids)s
                  AND (rd.manual_flag in (1,4) OR (rd.manual_flag IS NULL AND rd.quality_flag in (1,4)))
                  AND NOT rd.is_daily
                GROUP BY 1,2,3
                UNION ALL
                SELECT 
                    cast((rd.datetime + interval '%(offset)s minutes') at time zone 'utc' as DATE) as "date",
                    station_id,
                    variable_id,
                    min(calc.value) AS min_value,
                    max(calc.value) AS max_value,
                    avg(calc.value) AS avg_value,
                    sum(calc.value) AS sum_value,
                    count(calc.value) AS num_records,
                    now(),
                    now()
                FROM 
                    raw_data rd
                    ,LATERAL (SELECT CASE WHEN rd.consisted IS NOT NULL THEN rd.consisted ELSE rd.measured END as value) AS calc
                WHERE rd.datetime > %(datetime_start)s
                  AND rd.datetime <= %(datetime_end)s
                  AND calc.value != %(MISSING_VALUE)s
                  AND station_id in %(station_ids)s
                  AND (rd.manual_flag in (1,4) OR (rd.manual_flag IS NULL AND rd.quality_flag in (1,4)))
                  AND rd.is_daily
                GROUP BY 1,2,3
            """

            cursor.execute(delete_sql, {"datetime_start": datetime_start_utc, "datetime_end": datetime_end_utc,
                                        "station_ids": station_ids})
            cursor.execute(insert_sql,
                           {"datetime_start": datetime_start, "datetime_end": datetime_end, "station_ids": station_ids,
                            "offset": offset, "MISSING_VALUE": settings.MISSING_VALUE})
            conn.commit()

    conn.commit()
    conn.close()

    cache.set('daily_summary_last_run', datetime.today(), None)
    logger.info(f'Daily summary finished at {datetime.now(pytz.UTC)}. Took {time() - start_at} seconds.')


@shared_task
def calculate_station_minimum_interval(start_date=None, end_date=None, station_id_list=None):
    logger.info(f'CALCULATE STATION MINIMUM INTERVAL started at {datetime.now(tz=pytz.UTC)} with parameters: '
                f'start_date={start_date} end_date={end_date} '
                f'station_id_list={station_id_list}')

    start_at = time()

    if start_date is None or end_date is None:
        start_date = datetime.now(pytz.UTC).date()
        end_date = (datetime.now(pytz.UTC) + timedelta(days=1)).date()

    if start_date > end_date:
        print('Error - start_date is more recent than end_date.')
        return

    conn = get_connection()
    with conn.cursor() as cursor:

        if station_id_list is None:
            stations = Station.objects.filter(is_active=True)
        else:
            stations = Station.objects.filter(id__in=station_id_list)

        offsets = list(set([s.utc_offset_minutes for s in stations]))
        for offset in offsets:
            station_ids = tuple(stations.filter(utc_offset_minutes=offset).values_list('id', flat=True))
            fixed_offset = pytz.FixedOffset(offset)

            datetime_start_utc = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=pytz.UTC)
            datetime_end_utc = datetime(end_date.year, end_date.month, end_date.day, 0, 0, 0, tzinfo=pytz.UTC)

            datetime_start = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0,
                                      tzinfo=fixed_offset).astimezone(pytz.UTC)
            datetime_end = datetime(end_date.year, end_date.month, end_date.day, 0, 0, 0,
                                    tzinfo=fixed_offset).astimezone(pytz.UTC)

            logger.info(f"datetime_start={datetime_start}, datetime_end={datetime_end} "
                        f"offset={offset} "
                        f"station_ids={station_ids}")

            insert_minimum_data_interval = """
                INSERT INTO wx_stationdataminimuminterval (
                     datetime
                    ,station_id
                    ,variable_id
                    ,minimum_interval
                    ,record_count
                    ,ideal_record_count
                    ,record_count_percentage
                    ,created_at
                    ,updated_at
                ) 
                SELECT current_day
                      ,stationvariable.station_id
                      ,stationvariable.variable_id
                      ,min(value.data_interval) as minimum_interval
                      ,COALESCE(count(value.formated_datetime), 0) as record_count 
                      ,COALESCE(EXTRACT('EPOCH' FROM interval '1 day') / EXTRACT('EPOCH' FROM min(value.data_interval)), 0) as ideal_record_count
                      ,COALESCE(count(value.formated_datetime) / (EXTRACT('EPOCH' FROM interval '1 day') / EXTRACT('EPOCH' FROM min(value.data_interval))) * 100, 0) as record_count_percentage
                      ,now()
                      ,now()
                FROM generate_series(%(datetime_start)s , %(datetime_end)s , INTERVAL '1 day') as current_day
                    ,wx_stationvariable as stationvariable
                    ,wx_station as station
                LEFT JOIN LATERAL (
                    SELECT date_trunc('day', rd.datetime - INTERVAL '1 second' + (COALESCE(station.utc_offset_minutes, 0)||' minutes')::interval) as formated_datetime
                          ,CASE WHEN rd.is_daily THEN '24:00:00' ELSE LEAD(datetime, 1) OVER (partition by station_id, variable_id order by datetime) - datetime END as data_interval
                    FROM raw_data rd
                    WHERE rd.datetime   > current_day - ((COALESCE(station.utc_offset_minutes, 0)||' minutes')::interval)
                      AND rd.datetime   <= current_day + INTERVAL '1 DAY' - ((COALESCE(station.utc_offset_minutes, 0)||' minutes')::interval)
                      AND rd.station_id  = stationvariable.station_id
                      AND rd.variable_id = stationvariable.variable_id
                ) value ON TRUE
                WHERE stationvariable.station_id IN %(station_ids)s
                  AND stationvariable.station_id = station.id
                  AND (value.formated_datetime = current_day OR value.formated_datetime is null)
                GROUP BY current_day, stationvariable.station_id, stationvariable.variable_id
                  ON CONFLICT (datetime, station_id, variable_id)
                  DO UPDATE SET
                     minimum_interval        = excluded.minimum_interval
                    ,record_count            = excluded.record_count
                    ,ideal_record_count      = excluded.ideal_record_count
                    ,record_count_percentage = excluded.record_count_percentage
                    ,updated_at = now()
            """
            cursor.execute(insert_minimum_data_interval,
                           {"datetime_start": datetime_start_utc, "datetime_end": datetime_end_utc,
                            "station_ids": station_ids})
            conn.commit()

    conn.commit()
    conn.close()

    logger.info(f'Calculate minimum interval finished at {datetime.now(pytz.UTC)}. Took {time() - start_at} seconds.')


@shared_task
def calculate_last24h_summary():
    print('Last 24h summary started at {}'.format(datetime.today()))

    conn = get_connection()

    with conn.cursor() as cursor:
        sql_delete = "DELETE FROM last24h_summary"
        print(sql_delete)
        cursor.execute(sql_delete)

        sql_insert = f"""
            INSERT INTO last24h_summary (
                datetime,
                station_id,
                variable_id,
                min_value,
                max_value,
                avg_value,
                sum_value,
                num_records,
                latest_value
            )
            WITH last AS (
                SELECT
                    datetime,
                    station_id,
                    variable_id,
                    latest_value
                FROM
                    (SELECT 
                        datetime,
                        station_id,
                        variable_id,
                        calc.value AS latest_value,
                        row_number() over (partition by station_id, variable_id order by datetime desc) as rownum
                    FROM 
                        raw_data rd
                        ,LATERAL (SELECT CASE WHEN rd.consisted IS NOT NULL THEN rd.consisted ELSE rd.measured END as value) AS calc
                    WHERE datetime > (now() - interval '1 day')
                    AND datetime <= now()
                    AND (rd.consisted IS NOT NULL OR quality_flag in (1, 4))
                    AND measured != {settings.MISSING_VALUE}
                    AND is_daily = false) AS latest
                WHERE
                    rownum = 1
            ),
            agg AS (
                SELECT 
                    station_id,
                    variable_id,
                    min(calc.value) AS min_value,
                    max(calc.value) AS max_value,
                    avg(calc.value) AS avg_value,
                    sum(calc.value) AS sum_value,
                    count(calc.value) AS num_records
                FROM 
                    raw_data rd
                    ,LATERAL (SELECT CASE WHEN rd.consisted IS NOT NULL THEN rd.consisted ELSE rd.measured END as value) AS calc
                WHERE datetime >  (now() - interval '1 day')
                  AND datetime <= now()
                  AND (rd.consisted IS NOT NULL OR quality_flag in (1, 4))
                  AND calc.value != {settings.MISSING_VALUE}
                  AND is_daily = false
                GROUP BY 1,2
            )
            SELECT
                now(),
                agg.station_id,
                agg.variable_id,
                agg.min_value,
                agg.max_value,
                agg.avg_value,
                agg.sum_value,
                agg.num_records,
                last.latest_value
            FROM
                agg
                JOIN last ON agg.station_id = last.station_id AND agg.variable_id = last.variable_id
            ON CONFLICT (station_id, variable_id) DO
            UPDATE SET
                min_value = excluded.min_value,
                max_value = excluded.max_value,
                avg_value = excluded.avg_value,
                sum_value = excluded.sum_value,
                num_records = excluded.num_records,
                latest_value = excluded.latest_value,
                datetime = excluded.datetime;
        """
        print(sql_insert)
        cursor.execute(sql_insert)

    conn.commit()
    conn.close()

    cache.set('last24h_summary_last_run', datetime.today(), None)
    print('Last 24h summary finished at {}'.format(datetime.today()))


@shared_task
def calculate_step_qc_test():
    print('Inside calculate_step_qc_test')

    conn = get_connection()

    with conn.cursor() as cursor:
        cursor.execute(f'''
        DO $$DECLARE rec record;
        BEGIN
            FOR rec in
                SELECT value.station_id
                      ,value.variable_id
                      ,value.datetime
                      ,COALESCE(ABS(value.measured - LAG(value.measured,1) OVER (PARTITION BY value.station_id, value.variable_id ORDER BY value.station_id, value.variable_id, value.datetime)), 0) step_result
                      ,station_var.test_step_value
                      ,value.qc_persist_quality_flag
                FROM raw_data as value
                LEFT JOIN wx_stationvariable station_var ON value.variable_id=station_var.variable_id and value.station_id=station_var.station_id
                WHERE station_var.test_step_value IS NOT NULL
            LOOP
                IF rec.step_result > rec.test_step_value THEN
                    IF rec.qc_persist_quality_flag = 2 THEN -- SUSPICIOUS
                        UPDATE raw_data
                        SET qc_step_quality_flag = 2
                           ,quality_flag = false
                           ,qc_step_description = FORMAT('The current step "%s" is bigger than the registered step value for this station and variable "%s".', rec.step_result,  rec.test_step_value)
                        WHERE rec.station_id  = station_id
                          AND rec.variable_id = variable_id
                          AND rec.datetime = datetime;
                    ELSE
                        UPDATE raw_data
                        SET qc_step_quality_flag = 2 -- SUSPICIOUS
                           ,qc_step_description = FORMAT('The current step "%s" is bigger than the registered step value for this station and variable "%s".', rec.step_result,  rec.test_step_value)
                        WHERE rec.station_id  = station_id
                          AND rec.variable_id = variable_id
                          AND rec.datetime = datetime;
                    END IF;
                ELSE
                    UPDATE raw_data
                    SET qc_step_quality_flag = 4
                       ,qc_step_description = FORMAT('The current step "%s" is smaller than the registered step value for this station and variable "%s".', rec.step_result,  rec.test_step_value)
                    WHERE rec.station_id  = station_id
                      AND rec.variable_id = variable_id
                      AND rec.datetime = datetime;
                END IF;
            END LOOP;

            UPDATE raw_data as value
            SET qc_step_quality_flag = 1
               ,qc_step_description = NULL
            WHERE not exists (select 1 from wx_stationvariable station_var where station_var.variable_id = value.variable_id and station_var.station_id = value.station_id);

            UPDATE raw_data as value
            SET qc_step_quality_flag = 1
               ,qc_step_description = NULL
            FROM wx_stationvariable station_var
            WHERE station_var.variable_id = value.variable_id
              AND station_var.station_id  = value.station_id
              AND station_var.test_step_value IS NULL;
        END$$;
        ''')

    conn.commit()

    conn.close()


@shared_task
def calculate_persist_qc_test():
    print('Inside calculate_persist_qc_test')

    conn = get_connection()

    with conn.cursor() as cursor:
        cursor.execute(f'''
        DO $$DECLARE rec record;
        BEGIN
            FOR rec in
                SELECT value.station_id
                      ,value.variable_id
                      ,value.datetime
                      ,COALESCE((select VARIANCE(past24hvalue.measured) from raw_data as past24hvalue where value.station_id=past24hvalue.station_id and value.variable_id=past24hvalue.variable_id and past24hvalue.datetime < value.datetime and past24hvalue.datetime >= value.datetime - INTERVAL '1 day'),0) current_variance
                      ,station_var.test_persistence_variance
                FROM raw_data as value
                INNER JOIN wx_stationvariable station_var ON value.variable_id=station_var.variable_id and value.station_id=station_var.station_id
                WHERE station_var.test_persistence_variance IS NOT NULL
                ORDER BY value.station_id, value.variable_id, value.datetime
            LOOP
                IF rec.current_variance > rec.test_persistence_variance THEN
                    UPDATE raw_data as past24hvalue
                    SET qc_persist_quality_flag = 2
                       ,qc_persist_description = FORMAT('This record belongs to a 24h series that result on a variance "%s" bigger than registered for this station and variable "%s".', rec.current_variance,  rec.test_persistence_variance)
                    WHERE rec.station_id  = past24hvalue.station_id
                      AND rec.variable_id = past24hvalue.variable_id
                      AND rec.datetime > past24hvalue.datetime
                      AND past24hvalue.datetime > rec.datetime - INTERVAL '1 day';
                ELSE
                    UPDATE raw_data
                    SET qc_persist_quality_flag = 4
                       ,qc_persist_description = FORMAT('This record belongs to a 24h series that result on a variance "%s" smaller than registered for this station and variable "%s".', rec.current_variance,  rec.test_persistence_variance)
                    WHERE rec.station_id  = station_id
                      AND rec.variable_id = variable_id
                      AND rec.datetime = datetime;
                END IF;
            END LOOP;


            UPDATE raw_data as value
            SET qc_persist_quality_flag = 1
               ,qc_persist_description = NULL
            WHERE not exists (select 1 from wx_stationvariable station_var where station_var.variable_id = value.variable_id and station_var.station_id = value.station_id);

            UPDATE raw_data as value
            SET qc_persist_quality_flag = 1
               ,qc_persist_description = NULL
            FROM wx_stationvariable station_var
            WHERE station_var.variable_id = value.variable_id
              AND station_var.station_id  = value.station_id
              AND station_var.test_persistence_variance IS NULL;
        END$$;
        ''')

    conn.commit()

    conn.close()


@shared_task
def process_document():
    available_decoders = {
        'HOBO': read_file_hobo,
        'TOA5': read_file,
        'HYDROLOGY': read_file_hydrology
        # Nesa
    }

    default_decoder = 'TOA5'

    document_list = Document.objects.select_related('decoder', 'station').filter(processed=False).order_by('id')[:60]

    logger.info('Documents: %s' % document_list)

    for document in document_list:
        if document.decoder:
            current_decoder = available_decoders[document.decoder.name]
        else:
            current_decoder = available_decoders[default_decoder]

        logger.info('Processing file "{0}" with "{1}" decoder.'.format(document.file.path, current_decoder))

        try:
            current_decoder(document.file.path, document.station)
        except Exception as err:
            logger.error(
                'Error Processing file "{0}" with "{1}" decoder. '.format(document.file.path, current_decoder) + repr(
                    err))
            db_logger.error(
                'Error Processing file "{0}" with "{1}" decoder. '.format(document.file.path, current_decoder) + repr(
                    err))
        else:
            document.processed = True
            document.save()


@shared_task
def dcp_tasks_scheduler():
    logger.info('Inside dcp_tasks_scheduler')

    noaa_list_to_process = []
    for noaaDcp in NoaaDcp.objects.all():
        now = pytz.UTC.localize(datetime.now())

        if noaaDcp.last_datetime is None:
            next_execution = now
        else:
            scheduled_execution = datetime(year=now.year,
                                           month=now.month,
                                           day=now.day,
                                           hour=now.hour,
                                           minute=noaaDcp.first_transmission_time.minute,
                                           second=noaaDcp.first_transmission_time.second)

            transmission_window_timedelta = timedelta(minutes=noaaDcp.transmission_window.minute,
                                                      seconds=noaaDcp.transmission_window.second)

            next_execution = scheduled_execution + transmission_window_timedelta
            next_execution = pytz.UTC.localize(next_execution)

        if next_execution <= now and (noaaDcp.last_datetime is None or noaaDcp.last_datetime < next_execution):
            noaa_list_to_process.append({"noaa_object": noaaDcp, "last_execution": noaaDcp.last_datetime})
            noaaDcp.last_datetime = now
            noaaDcp.save()

    for noaa_dcp in noaa_list_to_process:
        try:
            retrieve_dpc_messages(noaa_dcp)
        except Exception as e:
            logging.error(f'dcp_tasks_scheduler ERROR: {repr(e)}')


def retrieve_dpc_messages(noaa_dict):
    current_noaa_dcp = noaa_dict["noaa_object"]
    last_execution = noaa_dict["last_execution"]

    logger.info('Inside retrieve_dpc_messages ' + current_noaa_dcp.dcp_address)

    related_stations = current_noaa_dcp.noaadcpsstation_set
    related_stations_count = related_stations.count()
    if related_stations_count == 0:
        raise Exception(f"The noaa dcp '{current_noaa_dcp}' is not related to any Station.")
    elif related_stations_count != 1:
        raise Exception(f"The noaa dcp '{current_noaa_dcp}' is related to more than one Station.")

    noaa_dcp_station = related_stations.first()
    station_id = noaa_dcp_station.station_id
    decoder = noaa_dcp_station.decoder.name

    available_decoders = {
        'NESA': read_data_nesa,
    }

    set_search_criteria(current_noaa_dcp, last_execution)

    command = subprocess.Popen([settings.LRGS_EXECUTABLE_PATH,
                                '-h', settings.LRGS_SERVER_HOST,
                                '-p', settings.LRGS_SERVER_PORT,
                                '-u', settings.LRGS_USER,
                                '-P', settings.LRGS_PASSWORD,
                                '-f', settings.LRGS_CS_FILE_PATH
                                ], shell=False, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

    output, err_message = command.communicate()
    response = output.decode('ascii')
    try:
        available_decoders[decoder](station_id, current_noaa_dcp.dcp_address, response, err_message)
    except Exception as err:
        logger.error(f'Error on retrieve_dpc_messages for dcp address "{current_noaa_dcp.dcp_address}". {repr(err)}')


def set_search_criteria(dcp, last_execution):
    with open(settings.LRGS_CS_FILE_PATH, 'w') as cs_file:
        if dcp.first_channel is not None:
            cs_file.write(
                f"""DRS_SINCE: now - {dcp_query_window(last_execution)} hour\nDRS_UNTIL: now\nDCP_ADDRESS: {dcp.dcp_address}\nCHANNEL: |{dcp.first_channel}\n""")
        else:
            cs_file.write(
                f"""DRS_SINCE: now - {dcp_query_window(last_execution)} hour\nDRS_UNTIL: now\nDCP_ADDRESS: {dcp.dcp_address}\n""")


def dcp_query_window(last_execution):
    return max(3, min(latest_received_dpc_data_in_hours(last_execution), int(settings.LRGS_MAX_INTERVAL)))


def latest_received_dpc_data_in_hours(last_execution):
    try:
        return int(((datetime.now().astimezone(pytz.UTC) - last_execution).total_seconds()) / 3600)
    except TypeError as e:
        return 999999


@shared_task
def get_entl_data():
    print('LIGHTNING DATA - Starting get_entl_data task...')
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as entl_socket:
            try:
                print("LIGHTNING DATA - Connecting to ENTLN server: {}:{}".format(settings.ENTL_PRIMARY_SERVER_HOST,
                                                                                  settings.ENTL_PRIMARY_SERVER_PORT))

                entl_socket.connect((settings.ENTL_PRIMARY_SERVER_HOST, settings.ENTL_PRIMARY_SERVER_PORT))
                entl_socket.settimeout(60)
                entl_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                entl_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 1)
                entl_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
                entl_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)

                print("LIGHTNING DATA - Authenticating...")
                auth_string = '{"p":"%s","v":3,"f":2,"t":1}' % settings.ENTL_PARTNER_ID
                entl_socket.send(auth_string.encode('latin-1'))
                print("LIGHTNING DATA - Authenticated")

                process_received_data(entl_socket)

            except Exception as e:
                print("LIGHTNING DATA - An error occurred: " + repr(
                    e) + "\nLIGHTNING DATA - Reconnecting in 3 seconds...")
                sleep(3)
        print('LIGHTNING DATA - Connection error. Reconnecting in 15 seconds...')
        sleep(15)


def process_received_data(entl_socket):
    while True:
        data = entl_socket.recv(56)
        if not data:
            return

        if data[1] == '9':
            print("LIGHTNING DATA - Keep alive packet")
        else:
            latitude = int.from_bytes(data[10:14], byteorder='big', signed=True) / 1e7
            longitude = int.from_bytes(data[14:18], byteorder='big', signed=True) / 1e7
            if 15 <= latitude <= 19 and -90 <= longitude <= -87:
                save_flash_data.delay(data.decode('latin-1'))


# Used to save Flash data asynchronously
@shared_task
def save_flash_data(data_string):
    read_data_flash(data_string.encode('latin-1'))


@shared_task
def export_data(station_id, source, start_date, end_date, variable_ids, file_id):
    logger.info(f'Exporting data (file "{file_id}")')

    timezone_offset = pytz.timezone(settings.TIMEZONE_NAME)
    start_date_utc = pytz.UTC.localize(datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S'))
    end_date_utc = pytz.UTC.localize(datetime.strptime(end_date, '%Y-%m-%d %H:%M:%S'))

    station = Station.objects.get(pk=station_id)
    current_datafile = DataFile.objects.get(pk=file_id)
    variable_ids = tuple(variable_ids)

    # Different data sources have different columns names for the measurement data and different intervals
    if source == 'raw_data':
        datetime_variable = 'datetime'
        data_source_description = 'Raw data'
        converted_start_date = start_date_utc
        converted_end_date = end_date_utc
    else:
        measured_source = '''
            CASE WHEN var.sampling_operation_id in (1,2) THEN data.avg_value
                 WHEN var.sampling_operation_id = 3      THEN data.min_value
                 WHEN var.sampling_operation_id = 4      THEN data.max_value
                 WHEN var.sampling_operation_id = 6      THEN data.sum_value
            ELSE data.sum_value END as value '''
        if source == 'hourly_summary':
            datetime_variable = 'datetime'
            date_source = f"(datetime + interval '{station.utc_offset_minutes} minutes') at time zone 'utc' as date"
            data_source_description = 'Hourly summary'
            converted_start_date = start_date_utc
            converted_end_date = end_date_utc
        elif source == 'daily_summary':
            datetime_variable = 'day'
            data_source_description = 'Daily summary'
            date_source = "day::date"
            converted_start_date = start_date_utc.astimezone(timezone_offset).date()
            converted_end_date = end_date_utc.astimezone(timezone_offset).date()
        elif source == 'monthly_summary':
            measured_source = '''
                CASE WHEN var.sampling_operation_id in (1,2) THEN data.avg_value::real
                    WHEN var.sampling_operation_id = 3      THEN data.min_value
                    WHEN var.sampling_operation_id = 4      THEN data.max_value
                    WHEN var.sampling_operation_id = 6      THEN data.sum_value
                ELSE data.sum_value END as value '''
            datetime_variable = 'date'
            date_source = "date::date"
            data_source_description = 'Monthly summary'
            converted_start_date = start_date_utc.astimezone(timezone_offset).date()
            converted_end_date = end_date_utc.astimezone(timezone_offset).date()
        elif source == 'yearly_summary':
            measured_source = '''
                CASE WHEN var.sampling_operation_id in (1,2) THEN data.avg_value::real
                    WHEN var.sampling_operation_id = 3      THEN data.min_value
                    WHEN var.sampling_operation_id = 4      THEN data.max_value
                    WHEN var.sampling_operation_id = 6      THEN data.sum_value
                ELSE data.sum_value END as value '''
            datetime_variable = 'date'
            date_source = "date::date"
            data_source_description = 'Yearly summary'
            converted_start_date = start_date_utc.astimezone(timezone_offset).date()
            converted_end_date = end_date_utc.astimezone(timezone_offset).date()

    try:
        variable_dict = {}
        variable_names_string = ''
        with connection.cursor() as cursor_variable:
            cursor_variable.execute(f'''
                SELECT var.symbol
                    ,var.id
                    ,CASE WHEN unit.symbol IS NOT NULL THEN CONCAT(var.symbol, ' - ', var.name, ' (', unit.symbol, ')') 
                        ELSE CONCAT(var.symbol, ' - ', var.name) END as var_name
                FROM wx_variable var 
                LEFT JOIN wx_unit unit ON var.unit_id = unit.id 
                WHERE var.id in %s
                ORDER BY var.name
            ''', (variable_ids,))

            rows = cursor_variable.fetchall()
            for row in rows:
                variable_dict[row[1]] = row[0]
                variable_names_string += f'{row[2]}   '

        # Iterate over the start and end date day by day to split the queries
        datetime_list = [converted_start_date]
        current_datetime = converted_start_date
        while current_datetime < converted_end_date and (current_datetime + timedelta(days=1)) < converted_end_date:
            current_datetime = current_datetime + timedelta(days=1)
            datetime_list.append(current_datetime)
        datetime_list.append(converted_end_date)

        query_result = []
        for i in range(0, len(datetime_list) - 1):
            current_start_datetime = datetime_list[i]
            current_end_datetime = datetime_list[i + 1]

            with connection.cursor() as cursor:

                if source == 'raw_data':
                    cursor.execute(f'''
                        WITH processed_data AS (
                            SELECT datetime
                                ,var.id as variable_id
                                ,CASE WHEN var.variable_type ilike 'code' THEN data.code ELSE data.measured::varchar END AS value
                            FROM raw_data data
                            JOIN wx_variable var ON data.variable_id = var.id AND var.id IN %(variable_ids)s
                            WHERE data.datetime >= %(start_datetime)s
                            AND data.datetime < %(end_datetime)s
                            AND data.station_id = %(station_id)s
                        )
                        SELECT (generated_time + interval '%(utc_offset)s minutes') at time zone 'utc' as datetime
                            ,variable.id
                            ,value
                        FROM generate_series(%(start_datetime)s, %(end_datetime)s - INTERVAL '1 seconds', INTERVAL '%(data_interval)s seconds') generated_time
                        JOIN wx_variable variable ON variable.id IN %(variable_ids)s
                        LEFT JOIN processed_data ON datetime = generated_time AND variable.id = variable_id
                    ''', {'utc_offset': station.utc_offset_minutes, 'variable_ids': variable_ids,
                          'start_datetime': current_start_datetime, 'end_datetime': current_end_datetime,
                          'station_id': station_id, 'data_interval': current_datafile.interval_in_seconds})
                else:
                    cursor.execute(f'''
                        SELECT {date_source}, var.id, {measured_source}
                        FROM {source} data
                        JOIN wx_variable var ON data.variable_id = var.id AND var.id in %s
                        WHERE data.{datetime_variable} >= %s 
                        AND data.{datetime_variable} < %s
                        AND data.station_id = %s
                    ''', (variable_ids, current_start_datetime, current_end_datetime, station_id,))

                query_result = query_result + cursor.fetchall()

        filepath = f'{settings.EXPORTED_DATA_CELERY_PATH}{file_id}.csv'
        date_of_completion = datetime.utcnow()
        with open(filepath, 'w') as f:
            start_date_header = start_date_utc.astimezone(timezone_offset).strftime('%Y-%m-%d %H:%M:%S')
            end_date_header = end_date_utc.astimezone(timezone_offset).strftime('%Y-%m-%d %H:%M:%S')

            f.write(f'Station:,{station.code} - {station.name}\n')
            f.write(f'Data source:,{data_source_description}\n')
            f.write(f'Description:,{variable_names_string}\n')
            f.write(f'Latitude:,{station.latitude}\n')
            f.write(f'Longitude:,{station.longitude}\n')
            f.write(f'Date of completion:,{date_of_completion.strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(f'Prepared by:,{current_datafile.prepared_by}\n')
            f.write(f'Start date:,{start_date_header},End date:,{end_date_header}\n\n')

        lines = 0
        if query_result:
            df = pandas.DataFrame(data=query_result).pivot(index=0, columns=1)
            df.rename(columns=variable_dict, inplace=True)
            df.columns = df.columns.droplevel(0)

            df['Year'] = df.index.map(lambda x: x.strftime('%Y'))
            df['Month'] = df.index.map(lambda x: x.strftime('%m'))
            df['Day'] = df.index.map(lambda x: x.strftime('%d'))
            df['Time'] = df.index.map(lambda x: x.strftime('%H:%M:%S'))
            cols = df.columns.tolist()
            cols = cols[-4:] + cols[:-4]
            df = df[cols]

            df.to_csv(filepath, index=False, mode='a', header=True)
            lines = len(df.index)

        current_datafile.ready = True
        current_datafile.ready_at = date_of_completion
        current_datafile.lines = lines
        current_datafile.save()
        logger.info(f'Data exported successfully (file "{file_id}")')
    except Exception as e:
        current_datafile.ready = False
        current_datafile.ready_at = datetime.utcnow()
        current_datafile.lines = 0
        current_datafile.save()
        logger.error(f'Error on export data file "{file_id}". {repr(e)}')


@shared_task
def ftp_ingest_historical_station_files():
    ftp_ingest_station_files(True)


@shared_task
def ftp_ingest_not_historical_station_files():
    ftp_ingest_station_files(False)


def ftp_ingest_station_files(historical_data):
    """
    Get and process station data files via FTP protocol

    Parameters: 
        historical_data (bool): flag to process historical station data files

    """
    dt = datetime.now()

    station_file_ingestions = StationFileIngestion.objects.filter(is_active=True, is_historical_data=historical_data)
    station_file_ingestions = [s for s in station_file_ingestions if
                               cronex.CronExpression(s.cron_schedule).check_trigger(
                                   (dt.year, dt.month, dt.day, dt.hour, dt.minute))]

    # List of unique ftp servers
    ftp_servers = list(set([s.ftp_server for s in station_file_ingestions]))

    # Loop over connecting to ftp servers, retrieving and processing files
    for ftp_server in ftp_servers:
        logging.info(f'Connecting to {ftp_server}')

        with FTP() as ftp:
            ftp.connect(ftp_server.host, ftp_server.port)
            ftp.login(ftp_server.username, ftp_server.password)
            ftp.set_pasv(not ftp_server.is_active_mode)
            home_folder = ftp.pwd()

            for sfi in [s for s in station_file_ingestions if s.ftp_server == ftp_server]:
                try:
                    ftp.cwd(sfi.remote_folder)
                except error_perm as e:
                    logger.error(f'Error on access the directory "{sfi.remote_folder}". {repr(e)}')
                    db_logger.error(f'Error on access the directory "{sfi.remote_folder}". {repr(e)}')

                # list remote files
                remote_files = ftp.nlst(sfi.file_pattern)

                for fname in remote_files:
                    try:
                        local_folder = '/data/documents/ingest/%s/%s/%04d/%02d/%02d' % (
                            sfi.decoder.name, sfi.station.code, dt.year, dt.month, dt.day)
                        local_filename = '%04d%02d%02d%02d%02d%02d_%s' % (
                            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, fname)
                        local_path = '%s/%s' % (local_folder, local_filename)
                        os.makedirs(local_folder, exist_ok=True)

                        hash_md5 = hashlib.md5()
                        if sfi.is_binary_transfer:
                            with open(local_path, 'wb') as fp_binary:
                                ftp.retrbinary(f'RETR {fname}',
                                               lambda data: [fp_binary.write(data), hash_md5.update(data)])
                        else:
                            with open(local_path, 'w') as fp:
                                ftp.retrlines(f'RETR {fname}', lambda line: [fp.write(line + '\n'),
                                                                             hash_md5.update(line.encode('utf8'))])

                        if sfi.delete_from_server:
                            try:
                                ftp.delete(fname)
                            except error_perm as e:
                                logger.error(
                                    'Permission error on delete the ftp server file "{0}".'.format(local_path) + repr(
                                        e))
                                db_logger.error(
                                    'Permission error on delete the ftp server file "{0}".'.format(local_path) + repr(
                                        e))
                            except error_reply as e:
                                logger.error('Unknown reply received "{0}".'.format(local_path) + repr(e))
                                db_logger.error('Unknown reply received "{0}".'.format(local_path) + repr(e))

                        # Inserts a StationDataFile object with status = 1 (Not processed)
                        station_data_file = StationDataFile(station=sfi.station
                                                            , decoder=sfi.decoder
                                                            , status_id=1
                                                            , utc_offset_minutes=sfi.utc_offset_minutes
                                                            , filepath=local_path
                                                            , file_hash=hash_md5.hexdigest()
                                                            , file_size=os.path.getsize(local_path)
                                                            , is_historical_data=sfi.is_historical_data
                                                            , override_data_on_conflict=sfi.override_data_on_conflict)
                        station_data_file.save()
                        logging.info(f'Downloaded FTP file: {local_path}')
                    except OSError as e:
                        logger.error('OS error. ' + repr(e))
                        db_logger.error('OS error. ' + repr(e))

                ftp.cwd(home_folder)

    process_station_data_files(historical_data)


def process_station_data_files(historical_data=False, force_reprocess=False):
    """
    Process station data files

    Parameters: 
        historical_data (bool): flag to process historical files
        force_reprocess (bool): force file to be reprocessed, don't check if the file is already processed
    """

    available_decoders = {
        'HOBO': read_file_hobo,
        'TOA5': read_file,
        'HYDROLOGY': read_file_hydrology,
        'BELIZE MANUAL DAILY DATA': read_file_manual_data,
        'BELIZE MANUAL HOURLY DATA': read_file_manual_data_hourly,
        'SURFACE': read_file_surface,
    }

    # Get StationDataFile to process
    # Filter status id to process only StationDataFiles with code 1 (Not processed) or 6 (Reprocess)
    station_data_file_list = (StationDataFile.objects.select_related('decoder', 'station')
                                  .filter(status_id__in=(1, 6), is_historical_data=historical_data).order_by('id')[:60])
    logger.info('Station data files: %s' % station_data_file_list)

    # Mark all file as Being processed to avoid reprocess
    for station_data_file in station_data_file_list:
        # Update status id to 2 (Being processed)
        station_data_file.status_id = 2
        station_data_file.save()

    for station_data_file in station_data_file_list:
        # if force_reprocess is true, ignore if file already exist on the database
        if not force_reprocess:
            # Verify if the file was already processed
            # Check if exists some StationDataFilestatus
            # object with the same file_hash and status different than 4 (Error) or 5 (Skipped)
            file_already_processed = (StationDataFile.objects.filter(file_hash=station_data_file.file_hash)
                                      .exclude(id=station_data_file.id).exclude(status_id__in=(4, 5)).exists())
            if file_already_processed:
                # Update status id to 5 (Skipped)
                station_data_file.status_id = 5
                station_data_file.save()
                continue

        try:
            current_decoder = available_decoders[station_data_file.decoder.name]
            logger.info('Processing file "{0}" with "{1}" decoder.'.format(station_data_file.filepath, current_decoder))

            current_decoder(filename=station_data_file.filepath
                            , station_object=station_data_file.station
                            , utc_offset=station_data_file.utc_offset_minutes
                            , override_data_on_conflict=station_data_file.override_data_on_conflict)

        except Exception as err:
            # Update status id to 4 (Error)
            station_data_file.status_id = 4
            station_data_file.observation = ('Error Processing file with "{0}" decoder. '
                                             .format(current_decoder) + repr(err))[:1024]
            station_data_file.save()

            logger.error('Error Processing file "{0}" with "{1}" decoder. '
                         .format(station_data_file.filepath, current_decoder) + repr(err))
            db_logger.error('Error Processing file "{0}" with "{1}" decoder. '
                            .format(station_data_file.filepath, current_decoder) + repr(err))
        else:
            # Update status id to 3 (Processed)
            station_data_file.status_id = 3
            station_data_file.save()


@shared_task
def process_hourly_summary_tasks():
    # process only 500 hourly summaries per execution
    unprocessed_hourly_summary_datetimes = HourlySummaryTask.objects.filter(started_at=None).values_list('datetime',
                                                                                                         flat=True).distinct()[
                                           :501]
    for hourly_summary_datetime in unprocessed_hourly_summary_datetimes:

        start_datetime = hourly_summary_datetime
        end_datetime = hourly_summary_datetime + timedelta(hours=1)

        hourly_summary_tasks = HourlySummaryTask.objects.filter(started_at=None, datetime=hourly_summary_datetime)
        hourly_summary_tasks_ids = list(hourly_summary_tasks.values_list('id', flat=True))
        station_ids = list(hourly_summary_tasks.values_list('station_id', flat=True).distinct())

        try:
            HourlySummaryTask.objects.filter(id__in=hourly_summary_tasks_ids).update(
                started_at=datetime.now(tz=pytz.UTC))
            calculate_hourly_summary(start_datetime, end_datetime, station_id_list=station_ids)
        except Exception as err:
            logger.error(
                'Error calculation hourly summary for hour "{0}". '.format(hourly_summary_datetime) + repr(err))
            db_logger.error(
                'Error calculation hourly summary for hour "{0}". '.format(hourly_summary_datetime) + repr(err))
        else:
            HourlySummaryTask.objects.filter(id__in=hourly_summary_tasks_ids).update(
                finished_at=datetime.now(tz=pytz.UTC))


@shared_task
def process_daily_summary_tasks():
    # process only 500 daily summaries per execution
    unprocessed_daily_summary_dates = DailySummaryTask.objects.filter(started_at=None).values_list('date',
                                                                                                   flat=True).distinct()[
                                      :501]
    for daily_summary_date in unprocessed_daily_summary_dates:

        start_date = daily_summary_date
        end_date = start_date + timedelta(days=1)

        daily_summary_tasks = DailySummaryTask.objects.filter(started_at=None, date=daily_summary_date)
        daily_summary_tasks_ids = list(daily_summary_tasks.values_list('id', flat=True))
        station_ids = list(daily_summary_tasks.values_list('station_id', flat=True).distinct())

        try:
            DailySummaryTask.objects.filter(id__in=daily_summary_tasks_ids).update(started_at=datetime.now(tz=pytz.UTC))
            calculate_daily_summary(start_date, end_date, station_id_list=station_ids)
            # for station_id in station_ids:
            #    calculate_station_minimum_interval(start_date, end_date, station_id_list=(station_id,))

        except Exception as err:
            logger.error('Error calculation daily summary for day "{0}". '.format(daily_summary_date) + repr(err))
            db_logger.error('Error calculation daily summary for day "{0}". '.format(daily_summary_date) + repr(err))
        else:
            DailySummaryTask.objects.filter(id__in=daily_summary_tasks_ids).update(
                finished_at=datetime.now(tz=pytz.UTC))


def predict_data(start_datetime, end_datetime, prediction_id, station_ids, target_station_id, variable_id,
                 data_period_in_minutes, interval_in_minutes, result_mapping):
    data_frequency = (interval_in_minutes // data_period_in_minutes) - 1
    date_dict = {}

    logger.info(
        f"predict_data= start_datetime: {start_datetime}, end_datetime: {end_datetime}, station_ids: {station_ids}, variable_id: {variable_id}, data_period_in_minutes: {data_period_in_minutes}, interval_in_minutes: {interval_in_minutes}, data_frequency: {data_frequency}")
    query = """
        WITH acc_query AS (SELECT datetime
                                ,station_id
                                ,SUM(measured) OVER (PARTITION BY station_id ORDER BY datetime ROWS BETWEEN %(data_frequency)s PRECEDING AND CURRENT ROW) AS acc
                                ,LAG(datetime, %(data_frequency)s) OVER (PARTITION BY station_id ORDER BY datetime) AS earliest_datetime
                           FROM raw_data
                           WHERE station_id in %(station_ids)s
                             AND variable_id = %(variable_id)s
                             AND datetime   >= %(start_datetime)s
                             AND datetime   <= %(end_datetime)s
                             AND measured   != %(MISSING_VALUE)s)
        SELECT acc_query.datetime
              ,acc_query.station_id
              ,acc_query.acc
        FROM acc_query
        WHERE acc_query.datetime - acc_query.earliest_datetime < INTERVAL '%(interval_in_minutes)s MINUTES';
    """

    params = {
        "start_datetime": start_datetime,
        "end_datetime": end_datetime,
        "station_ids": station_ids,
        "variable_id": variable_id,
        "data_frequency": data_frequency,
        "interval_in_minutes": interval_in_minutes,
        "MISSING_VALUE": settings.MISSING_VALUE
    }

    formated_list = []
    conn = get_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, params)

        # Group records in a dictionary by datetime
        # rows[0] = datetime
        # rows[1] = station_id
        # rows[2] = acc

        rows = cursor.fetchall()
        if len(rows) == 0:
            raise Exception('No data found')

        for row in rows:
            current_datetime = row[0]
            current_station_id = row[1]
            current_value = row[2]

            if current_datetime not in date_dict:
                date_dict[current_datetime] = {}

            date_dict[current_datetime][current_station_id] = current_value

        # Validate if a datetime contains all stations measurements, calculate avg and format value 
        for datetime, station_data_dict in date_dict.items():

            current_record_station_ids = tuple(station_data_dict.keys())
            if any(station_id not in current_record_station_ids for station_id in station_ids):
                continue

            current_record_dict = {'datetime': datetime.isoformat()}
            station_count = 0
            value_acc = 0.0

            for station_id, measured in station_data_dict.items():
                current_record_dict[station_id] = measured
                station_count += 1
                value_acc += measured

            if station_count > 0:
                current_record_dict['avg'] = value_acc / station_count
                formated_list.append(current_record_dict)

    # Format output request data
    request_data = {
        "prediction_id": prediction_id,
        "data": formated_list,
    }

    logger.info(f'request_data: {repr(request_data)}')

    request = requests.post(settings.HYDROML_URL, json=request_data)

    if request.status_code != 200:
        logger.error(f'Error on predict data via HydroML, {request.status_code}')
        return

    formated_response = []
    response = json.loads(request.json())

    # Format predicted values
    for record in response:
        try:
            result = result_mapping[str(record['prediction'])]

            formated_response.append({
                "datetime": dateutil.parser.isoparse(record['datetime']),
                "target_station_id": target_station_id,
                "variable_id": variable_id,
                "result": result,
            })
        except KeyError as e:
            logger.error(
                f'Error on predict_data for prediction "{prediction_id}": Invalid mapping for result "{record["prediction"]}".')
            raise Exception(e)

    # Update records' labels
    try:
        with conn.cursor() as cursor:
            cursor.executemany(f"""
                UPDATE raw_data 
                SET ml_flag = %(result)s
                WHERE station_id = %(target_station_id)s
                  AND variable_id = %(variable_id)s 
                  AND datetime = %(datetime)s;
            """, formated_response)
        conn.commit()
    except Exception as e:
        logger.error(f'Error on update raw_data: {repr(e)}')


@shared_task
def predict_preciptation_data():
    hydroml_params = HydroMLPredictionStation.objects.all()

    end_datetime = datetime.utcnow()
    start_datetime = end_datetime - timedelta(hours=2, minutes=30)

    for hydroml_param in hydroml_params:
        current_prediction = hydroml_param.prediction
        logger.info(f"Processing Prediction: {current_prediction.name}")
        station_ids = tuple(hydroml_param.neighborhood.neighborhood_stations.all().values_list('station_id', flat=True))

        result_mapping = {}
        mappings = HydroMLPredictionMapping.objects.filter(hydroml_prediction_id=hydroml_param.id)
        for mapping in mappings:
            result_mapping[mapping.prediction_result] = mapping.quality_flag.id

        try:
            predict_data(start_datetime=start_datetime,
                         end_datetime=end_datetime,
                         prediction_id=current_prediction.hydroml_prediction_id,
                         station_ids=station_ids,
                         target_station_id=hydroml_param.target_station.id,
                         variable_id=current_prediction.variable_id,
                         data_period_in_minutes=hydroml_param.data_period_in_minutes,
                         interval_in_minutes=hydroml_param.interval_in_minutes,
                         result_mapping=result_mapping)
        except Exception as e:
            logger.error(f'Error on predict_preciptation_data for "{current_prediction.name}": {repr(e)}')

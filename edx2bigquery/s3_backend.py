"""
This File contains an implmentation of S3 storage, to get the tracking log files from there,
and then, used them to upload into Google Big Query.
"""
import gzip
import os
from zipfile import ZipFile

import boto3

import edx2bigquery_config


def get_simple_storage_service_client():
    """
    Returns the S3 client object.

    Returns:
        boto3.client object.
    """
    set_aws_environment_settings()

    return boto3.client('s3')


def download_object_and_save(object_key, local_path_to_save):
    """
    Downloads and saves the provided object name.

    Saves the file with the same object key name inside of the provided local_path_to_save value.

    Args:
        object_key: Key name of the object.
        local_path_to_save: Local path to save the result objects.
    Raises:
        Exception: If object_key or local_path_to_save were not provided.
    """
    if not object_key:
        raise Exception('No object key was provided to download and save it locally.')

    if not local_path_to_save:
        raise Exception('No local path was provided to save the object file.')

    bucket_name = getattr(edx2bigquery_config, 'AWS_BUCKET_NAME', '')

    s3_client = get_simple_storage_service_client()

    print('Downloading {} into {}'.format(object_key, local_path_to_save))

    s3_client.download_file(bucket_name, object_key, local_path_to_save)


def set_aws_environment_settings():
    """
    Sets the AWS settings from the config file into the current environment.
    """
    if not os.environ.get('AWS_ACCESS_KEY_ID'):
        os.environ['AWS_ACCESS_KEY_ID'] = getattr(edx2bigquery_config, 'AWS_ACCESS_KEY_ID', '')

    if not os.environ.get('AWS_SECRET_ACCESS_KEY'):
        os.environ['AWS_SECRET_ACCESS_KEY'] = getattr(edx2bigquery_config, 'AWS_SECRET_ACCESS_KEY', '')


def get_tracking_log_objects(bucket_name, start_date):
    """
    Finds and gets all the objects matched by the tracking log date string.

    It will search in each folder of the provided TRACKING_LOG_FILE_NAME_PREFIX value.

    Args:
        bucket_name: Name of the bucket to the get the tracking objects.
        start_date: String date to find the tracking log objects.
    Raises:
        Exception: If not bucket name was provided in the configuration file.
    """
    if not bucket_name:
        print('AWS_BUCKET_NAME must be specified in the configuration file.')
        raise Exception('Not bucket name provided')

    aws_client = get_simple_storage_service_client()

    if not getattr(edx2bigquery_config, 'TRACKING_LOG_FILE_NAME_PREFIX', ''):
        print('The TRACKING_LOG_FILE_NAME_PREFIX setting is required.')
        exit()

    print('Searching tracking logs in the bucket: {} and the path: {}'.format(
        bucket_name,
        getattr(edx2bigquery_config, 'TRACKING_LOG_FILE_NAME_PREFIX', ''),
    ))

    all_instance_folder = aws_client.list_objects(
        Bucket=bucket_name,
        Delimiter='/',
        Prefix=getattr(edx2bigquery_config, 'TRACKING_LOG_FILE_NAME_PREFIX', ''),
    )
    folder_paginator = aws_client.get_paginator('list_objects')
    logs_dir = getattr(edx2bigquery_config, 'TRACKING_LOGS_DIRECTORY', '')

    if not os.path.exists(logs_dir):
        os.mkdir(logs_dir)

    for folder in all_instance_folder.get('CommonPrefixes', []):
        prefix_file_name = '{}{}{}'.format(
            folder.get('Prefix'),
            getattr(edx2bigquery_config, 'TRACKING_LOG_FILE_NAME_PATTERN', ''),
            start_date,
        )
        paginator_result = folder_paginator.paginate(
            Bucket=bucket_name, Prefix=prefix_file_name, PaginationConfig={'MaxItems': 100}
        )

        for object_file in paginator_result.search('Contents'):
            if not object_file:
                continue

            object_key = object_file.get('Key', '')
            local_file_name = object_key.split('/')[-1]
            local_path_name = '{}/{}'.format(
                logs_dir,
                local_file_name,
            )
            download_object_and_save(object_key, local_path_name)


def get_sql_data_objects(bucket_name, start_date):
    """
    Gets the MYSQL data generated by edx-analytics-exporter from Amazon S3.

    Args:
        bucket_name: Name of the bucket to the get the MySQL data.
        start_date: String date to find the MySQL data.
    Raises:
        Exception: If not bucket name was provided in the configuration file.
    """
    if not bucket_name:
        raise Exception('AWS_BUCKET_NAME must be specified in the configuration file.')

    # .zip Since edx-analytics-exporter uploads a .zip file containing all the generated data.
    object_key_name = '{}/{}{}{}'.format(
        getattr(edx2bigquery_config, 'SQL_DATA_BUCKET_PATH', ''),
        getattr(edx2bigquery_config, 'SQL_FILE_NAME_PREFIX', ''),
        start_date,
        '.zip'
    )
    sql_data_local_dir = '{}/{}{}'.format(
        getattr(edx2bigquery_config, 'SQL_LOCAL_FOLDER', ''),
        getattr(edx2bigquery_config, 'SQL_FILE_NAME_PREFIX', ''),
        start_date,
    )
    my_path = os.path.dirname(os.path.realpath(__file__))
    parent_path = os.path.abspath(os.path.join(my_path, os.pardir))
    path_to_extract = '{}/{}'.format(
        parent_path,
        getattr(edx2bigquery_config, 'SQL_SOURCE_DATA_LOCAL_FOLDER', ''),
    )

    if not os.path.exists(getattr(edx2bigquery_config, 'SQL_LOCAL_FOLDER', '')):
        os.mkdir(getattr(edx2bigquery_config, 'SQL_LOCAL_FOLDER', ''))

    download_object_and_save(object_key_name, sql_data_local_dir)
    extract_sql_data_from_zip_file(sql_data_local_dir, path_to_extract)


def extract_sql_data_from_zip_file(zip_file_name, path_to_extract):
    """
    Extracts the MySQL data from the provide zip file in a folder called just like the name of the zip file.

    Args:
        zip_file_name: Name of the .zip file to extract.
        path_to_extract: Path to extract the contents of the provided .zip file. If the path does not exists
                         it will be created on your behalf.
    """
    if not os.path.exists(path_to_extract):
        os.mkdir(path_to_extract)

    print('Extracting file with name: {} into {}'.format(zip_file_name, path_to_extract))

    with ZipFile(zip_file_name, 'r') as zip_file:
        zip_file.extractall(path_to_extract)

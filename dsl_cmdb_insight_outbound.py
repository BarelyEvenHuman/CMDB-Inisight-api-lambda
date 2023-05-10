import boto3
import requests
import json
import os
import snowflake.connector
from pathlib import Path
from aws_lambda_powertools import Logger


logger = Logger(service="cmdb_insight_outbound")


INSIGHT_TOKEN = os.environ["INSIGHT_TOKEN"]
INFO_ENDPOINT = os.environ["INFO_ENDPOINT"]
SNOWFLAKE_SECRETS = os.environ["SNOWFLAKE_SECRETS"]


def get_secrets(sm):
    """Retrieves secrets for Snowflake credentials."""
    return json.loads(sm.get_secret_value(SecretId=SNOWFLAKE_SECRETS)["SecretString"])


def snomi_connection(secrets):
    conn = snowflake.connector.connect(
        user=secrets["SNOMIUSER"],
        password=secrets["SNOMIPASS"],
        account=secrets["SNOMIACCOUNT"],
        database="CMDB_DEV",
        schema="SANDBOX",
        warehouse="AWS_SVC_S_WH",
        role="AWS_SVC_DE_DEV",
    )
    return conn


def query_resources(cursor):
    try:
        query = """with
            base as (
                select
                    object_insert(recode_content,'nomi_ci_key_plain',recode_content:awsRegion::string||'::'||recode_content:awsAccountId::string||'::'||recode_content:resourceType::string||'::'||recode_content:resourceId::string) recode_content
                    ,row_number() over (partition by recode_content:awsRegion::string||'::'||recode_content:awsAccountId::string||'::'||recode_content:resourceType::string||'::'||recode_content:resourceId::string order by recode_content:configurationItemCaptureTime::datetime desc) row_num
                from cmdb_dev.sandbox.config_target_tabular --from cmdb_dev.sandbox.config_target_table
                    where date(file_date) = date(sysdate()) - 1
                        and arn <> 'NULL'
            )
            ,core as (
                select
                    object_insert(recode_content,'nomi_ci_key',sha2(recode_content:nomi_ci_key_plain,256)) recode_content
                from base where row_num=1
            )
            select * from core
            order by recode_content:"tags";"""
        cursor.execute(query)
        df = cursor.fetch_pandas_all()
        aws_resources_raw = json.loads(df.to_json(orient="records"))
        aws_resources = list(json.loads(x["RECODE_CONTENT"]) for x in aws_resources_raw)
        logger.info(f"Queried Snowflake, {len(aws_resources)} resources to process.")
        return aws_resources
    except Exception as e:
        logger.error(f"An error occurred while querying Snowflake: {e}")


def get_info_links(token, info_endpoint):
    """Used to return the end points needed
    for other calls."""
    try:
        response = requests.get(
            info_endpoint, headers={"Authorization": f"Bearer {token}"}
        )
        links = json.loads(response.text)["links"]
        logger.info(f"Retrieved info links. Response: {response}")
        return links["getStatus"], links["start"], links["mapping"]
    except Exception as e:
        logger.error(f"An error occurred while calling the info endpoint: {e}")


def get_status(token, getStatus):
    """Fetches status of mapping configuration. Returns the following:
    {
        "status": "IDLE"
    }
    If status is IDLE we are good to start an import.
    """
    for _ in range(5):
        try:
            response = requests.get(
                getStatus, headers={"Authorization": f"Bearer {token}"}
            )
            if response:
                status = json.loads(response.text)["status"]
                logger.info(f"Status: {status}")
                return json.loads(response.text)
            else:
                logger.error(
                    f"Status: {response.status_code}, Error: {response.text}. Retry Count: {_}"
                )
        except Exception as e:
            logger.error(f"An error occurred calling the Get Status endpoint: {e}")


def mapping_configuration(token, mapping):
    """Use to submit the schema and mapping configuration."""
    try:
        mapping_config = open("schema.json", "r")
        data = json.load(mapping_config)
        response = requests.put(
            mapping,
            headers={"Authorization": f"Bearer {token}"},
            json=data,
        )
        logger.info(f"Submitted mapping and configuration. Response: {response.text}")
        return response.text
    except Exception as e:
        logger.error(f"An error occurred calling the mapping endpoint: {e}")


def start_import(token, start):
    """Use to import the data from 1st lambda into api.
    This returns other links we can use to check the import's status."""
    try:
        response = requests.post(start, headers={"Authorization": f"Bearer {token}"})
        links = json.loads(response.text)["links"]
        logger.info(f"Started import. Response {response}")
        return links["submitResults"]
    except Exception as e:
        logger.error(f"An error occurred calling the start import endpoint: {e}")


def submit_results(token, submitResults, aws_resources):
    """This finally submits the data and tags it as completed.
    The new aws resources should be fully imported now."""
    try:
        chunked_aws_resources = list(chunks(aws_resources))
        session = requests.Session()
        payload = {"data": {"awsResources": None}}
        headers = {"Authorization": f"Bearer {token}"}
        for i, resource_chunk in enumerate(chunked_aws_resources):
            count = len(resource_chunk)
            payload["data"]["awsResources"] = resource_chunk
            response = session.post(
                submitResults,
                headers=headers,
                json=payload,
            )
            success = json.loads(response.text)
            logger.info(
                f"{count} resources were submitted. Chunk {i+1} of {len(chunked_aws_resources)}. Response: {success}"
            )
        complete = signal_completion(token, submitResults)
        logger.info(f"Finished sending data chunks, complete. {complete}")
        return complete
    except Exception as e:
        logger.error(f"An error occurred calling the submit results endpoint: {e}")


def signal_completion(token, submitResults):
    """Tells Insight api that all data has been sent.
    It will then start to process all data."""
    try:
        response = requests.post(
            submitResults,
            headers={"Authorization": f"Bearer {token}"},
            json={"completed": "true"},
        )
        complete = json.loads(response.text)
        return complete
    except Exception as e:
        logger.error(f"An error occurred calling the submit results endpoint: {e}")


def cancel_import(token, status):
    """Used to cancel the import if its
    stuck in 'running' status."""
    try:
        response = requests.delete(
            status, headers={"Authorization": f"Bearer {token}"}, data=""
        )
        logger.info(f"Cancelled import. Response: {response}")
        return response
    except Exception as e:
        logger.error(f"An error occurred calling the cancel import endpoint: {e}")


def submit_import_calls(token, start, aws_resources, getStatus):
    logger.info("Trying to submit data...")
    submitResults = start_import(token, start)
    submit_status = submit_results(token, submitResults, aws_resources)
    execution_status = get_status(INSIGHT_TOKEN, getStatus)
    execution = execution_status["links"]["getExecutionStatus"]
    logger.info(f"Execution Status Link: {execution}")
    return submit_status


def chunks(aws_resources, size=500):
    """Breaks up the data into chunks based on
    chunk size specified."""
    logger.info(f"Separating data in chunks of size: {size}")
    for resource in range(0, len(aws_resources), size):
        yield aws_resources[resource : resource + size]


def lambda_handler(event, context):
    logger.info("dsl_cmdb_insight_outbound lambda was triggered.")
    sm = boto3.client("secretsmanager")
    secrets = get_secrets(sm)
    with snomi_connection(secrets) as conn:
        cursor = conn.cursor()
        aws_resources = query_resources(cursor)
        if not aws_resources:
            logger.info("Query returned 0 results, terminating lambda.")
            return
        else:
            getStatus, start, mapping = get_info_links(INSIGHT_TOKEN, INFO_ENDPOINT)
            status = get_status(INSIGHT_TOKEN, getStatus)
            if status["status"] == "IDLE":
                submitResults = start_import(INSIGHT_TOKEN, start)
                submit_results(INSIGHT_TOKEN, submitResults, aws_resources)
                execution_status = get_status(INSIGHT_TOKEN, getStatus)
                execution = execution_status["links"]["getExecutionStatus"]
                logger.info(f"Execution Status Link: {execution}")
            elif status["status"] == "RUNNING":
                cancel = status["links"]["cancel"]
                cancel_import(INSIGHT_TOKEN, cancel)
                submit_import_calls(INSIGHT_TOKEN, start, aws_resources, getStatus)
            elif status["status"] == "MISSING_MAPPING":
                mapping_configuration(INSIGHT_TOKEN, mapping)
                submit_import_calls(INSIGHT_TOKEN, start, aws_resources, getStatus)
    logger.info("Lambda finished running.")

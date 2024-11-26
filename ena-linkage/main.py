import json
import logging
import os
import uuid
from typing import Dict, List

import requests
from kafka import KafkaConsumer, KafkaProducer
import shared

HAS_EVENT = "ods:hasEvents"

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)


def start_kafka() -> None:
    """
    Start a kafka listener and process the messages by unpacking the image.
    When done it will republish the object, so it can be validated and stored by the processing service
    """
    consumer = KafkaConsumer(
        os.environ.get("KAFKA_CONSUMER_TOPIC"),
        group_id=os.environ.get("KAFKA_CONSUMER_GROUP"),
        bootstrap_servers=[os.environ.get("KAFKA_CONSUMER_HOST")],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        enable_auto_commit=True,
    )
    producer = KafkaProducer(
        bootstrap_servers=[os.environ.get("KAFKA_PRODUCER_HOST")],
        value_serializer=lambda m: json.dumps(m).encode("utf-8"),
    )
    for msg in consumer:
        try:
            logging.info("Received message: " + str(msg.value))
            json_value = msg.value
            shared.mark_job_as_running(json_value.get("jobId"))
            specimen_data = json_value.get("object")
            result = run_api_call(specimen_data)
            mas_job_record = map_to_annotation_event(specimen_data, result, json_value.get("jobId"))
            publish_annotation_event(mas_job_record, producer)
        except Exception as e:
            logging.exception(e)


def map_to_annotation_event(specimen_data: Dict, results: List[Dict[str, str]], job_id: str) -> Dict:
    """
    Map the result of the API call to an annotation
    :param specimen_data: The JSON value of the Digital Specimen
    :param results: A list of results that contain the queryString and the geoCASe identifier
    :param job_id: The job ID of the MAS
    :return: Returns a formatted annotation Record which includes the Job ID
    """
    timestamp = shared.timestamp_now()
    if results is None:
        annotations = list()
    else:
        annotations = list(map(lambda result: map_result_to_annotation(specimen_data, result, timestamp), results))
    mas_job_record = {"jobId": job_id, "annotations": annotations}
    return mas_job_record


def map_result_to_annotation(specimen_data: Dict, result: Dict[str, str], timestamp: str) -> Dict:
    """
    Map the result of the API call to an annotation
    :param specimen_data: The original specimen data
    :param result: The result from ENA, contains the ENAAccessionId and the queryString
    :param timestamp: A formatted timestamp of the current time
    :return: Returns a formatted annotation Record
    """

    ods_agent = shared.get_agent()
    oa_value = shared.map_to_entity_relationship(
        "hasEnaAccessionNumber",
        result["enaAccessionId"],
        f'https://www.ebi.ac.uk/ena/browser/view/{result["enaAccessionId"]}',
        timestamp,
        ods_agent,
    )
    oa_selector = shared.build_class_selector("$['ods:hasEntityRelationships']")
    return shared.map_to_annotation(
        ods_agent,
        timestamp,
        oa_value,
        oa_selector,
        specimen_data[shared.ODS_ID],
        specimen_data[shared.ODS_TYPE],
        result["queryString"],
    )


def publish_annotation_event(annotation: Dict, producer: KafkaProducer) -> None:
    """
    Send the annotation to the Kafka topic
    :param annotation: The formatted annotationRecord
    :param producer: The initiated Kafka producer
    :return: Will not return anything
    """
    logging.info("Publishing annotation: " + str(annotation))
    producer.send(os.environ.get("KAFKA_PRODUCER_TOPIC"), annotation)


def run_additional_checks(response_json: Dict, specimen_data: Dict) -> bool:
    """
    Run additional checks to make sure that the response is correct
    :param response_json: ENA response json
    :param specimen_data: Original specimen data
    :return: Returns whether it passes for the checks (True) or not (False)
    """
    is_valid = False
    if specimen_data.get(HAS_EVENT)[0].get("dwc:eventDate") and response_json.get("collection_date"):
        if specimen_data.get(HAS_EVENT)[0].get("dwc:eventDate") == response_json.get("collection_date"):
            is_valid = True
        else:
            logging.info(
                f'Event date {response_json.get("collection_date")} does not match for specimen: '
                f'{specimen_data.get(HAS_EVENT)[0].get("dwc:eventDate")}'
            )
            is_valid = False
    if specimen_data.get(HAS_EVENT)[0].get("ods:hasLocation").get("dwc:country") and response_json.get("country"):
        if specimen_data.get(HAS_EVENT)[0].get("ods:hasLocation").get("dwc:country") in response_json.get("country"):
            is_valid = True
        else:
            logging.info(
                f'Country {response_json.get("country")} does not match for specimen: '
                f'{specimen_data.get(HAS_EVENT)[0].get("ods:hasLocation").get("dwc:country")}'
            )
            is_valid = False
    return is_valid


def run_api_call(specimen_data: Dict) -> List[Dict[str, str]]:
    """
    Calls ENA API based on the available identifiers, unitId and/or recordURI.
    It is possible that one Digital Specimen has multiple GeoCASe records.
    If we get more than 5 GeoCASe hits we assume that something went wrong, and we will not return any results.
    :param specimen_data: The JSON data of the Digital Specimen
    :return:  A list of results that contain the queryString and the geoCASe identifier
    """
    identifiers = list(
        map(lambda identifier: identifier.get("dcterms:identifierValue"), specimen_data.get("ods:hasIdentifiers"))
    )
    sequence_query = build_query_string(identifiers, "sequence")
    response = requests.get(sequence_query)
    response_json = json.loads(response.content)
    result_list = list()
    if len(response_json) > 0:
        check_result(response_json, result_list, sequence_query, specimen_data)
        return result_list
    else:
        sample_query = build_query_string(identifiers, "sample")
        response = requests.get(sample_query)
        response_json = json.loads(response.content)
        if len(response_json) > 0:
            check_result(response_json, result_list, sequence_query, specimen_data)
            return result_list
        else:
            logging.info(f'No relevant identifiers found for specimen: {specimen_data["dcterms:identifier"]}')


def check_result(
    response_json: Dict, result_list: List[Dict[str, str]], sequence_query: str, specimen_data: Dict
) -> None:
    """
    Check the result of the API call and add it to the result list if it passes the additional checks
    :param response_json: The ENA response json
    :param result_list: The list with successful results
    :param sequence_query: The query string that was used to retrieve the results
    :param specimen_data: The original specimen data
    :return: Adds the result to the result list, does not return anything
    """
    for result in response_json:
        if run_additional_checks(result, specimen_data):
            result_list.append({"queryString": sequence_query, "enaAccessionId": result["accession"]})


def build_query_string(identifiers: List[str], endpoint: str) -> str:
    """
    Build the query string for the ENA API based on all identifiers in the specimen data
    :param identifiers: All identifiers available in the specimen data
    :param endpoint: Whether we will check the sequence or the sample endpoint
    :return: Return the full query string which we will use to call the API
    """
    query_string = f"https://www.ebi.ac.uk/ena/portal/api/search?result={endpoint}&format=json&fields=all&query="
    if identifiers and len(identifiers) > 0:
        for identifier in identifiers:
            if not query_string.endswith("query="):
                query_string = query_string + " OR "
            query_string = query_string + (
                f'specimen_voucher="{identifier}" '
                f'OR bio_material="{identifier}" '
                f'OR culture_collection="{identifier}" '
                f'OR isolation_source="{identifier}"'
            )
    return query_string


def run_local(example: str) -> None:
    """
    Run the script locally. Can be called by replacing the kafka call with this  a method call in the main method.
    Will call the DiSSCo API to retrieve the specimen data.
    A record ID will be created but can only be used for testing.
    :param example: The full URL of the Digital Specimen to the API (for example
    https://dev.dissco.tech/api/v1/specimens/TEST/S0P-JMS-V4Q
    :return: Return nothing but will log the result
    """
    response = requests.get(example)
    specimen = json.loads(response.content).get("data")
    specimen_data = specimen.get("attributes")
    result = run_api_call(specimen_data)
    mas_job_record = map_to_annotation_event(specimen_data, result, str(uuid.uuid4()))
    logging.info("Created annotations: " + str(mas_job_record))


if __name__ == "__main__":
    start_kafka()
    # run_local('https://dev.dissco.tech/api/v1/digital-specimen/TEST/0M6-9K9-H5P')

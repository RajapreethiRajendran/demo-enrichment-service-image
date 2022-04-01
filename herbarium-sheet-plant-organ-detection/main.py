import json
import logging
import os

import requests as requests
from kafka import KafkaConsumer, KafkaProducer
from PIL import Image
import numpy as np
from datetime import datetime

from detectron2.config import get_cfg
from detectron2.engine.defaults import DefaultPredictor
from detectron2 import model_zoo
from detectron2.data.detection_utils import convert_PIL_to_numpy

logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)

def start_kafka(name: str, predictor: DefaultPredictor) -> None:
    """
    Start a kafka listener and process the messages by unpacking the image.
    When done it will republish the object, so it can be validated and storage by the processing service
    :param name: The topic name the Kafka listener needs to listen to
    """
    consumer = KafkaConsumer(name, group_id='group', bootstrap_servers=[os.environ.get('KAFKA_CONSUMER_HOST')],
                             value_deserializer=lambda m: json.loads(m.decode('utf-8')))
    producer = KafkaProducer(bootstrap_servers=[os.environ.get('KAFKA_PRODUCER_HOST')],
                             value_serializer=lambda v: json.dumps(v).encode('utf-8'))
    producer_topic = os.environ.get('KAFKA_PRODUCER_TOPIC')
    logging.info("Starting consumer for topic: %s", name)
    for msg in consumer:
        logging.info(msg.value)
        json_value = msg.value
        for image in json_value['ods:images']:
            image_uri = image['ods:imageURI']
            additional_info_annotations = run_object_detection(image_uri,
                                                            predictor)
            if 'additional_info' in image and type(image['additional_info']) == list:
                image['additional_info'].append(additional_info_annotations)
            else:
                image['additional_info'] = [additional_info_annotations]
        logging.info("Publishing the result: %s", json_value)
        producer.send(producer_topic, json_value)


def run_object_detection(image_uri: str, predictor: DefaultPredictor) -> list:
    """
    Checks if the Image url works and gathers metadata information from the image
    :param image_uri: The image url from which we will gather metadata
    :return: Returns a list of additional info about the image
    """
    try:
        img = Image.open(requests.get(image_uri, stream=True).raw)
        predictions = predictor(np.array(img))
        instances = predictions['instances']
        annotations_result = []

        class_names = ['leaf', 'flower', 'fruit', 'seed', 'stem', 'root']
        """
        Per template these are according to model training (pay attention to the order!):
        https://github.com/2younis/plant-organ-detection/blob/master/train_net.py
        """
        boxes = instances.pred_boxes.tensor.numpy()
        classes = instances.pred_classes
        scores = instances.scores.numpy()
        num_instances = len(boxes)
        logging.info('Detected %d instances' % num_instances)
        for i in range(num_instances):
            annotations_result.append({
                'class': class_names[classes[i]],
                'score': float(scores[i]),
                'boundingBox': [int(x) for x in boxes[i]]
            })

        additional_info_annotations = {
            'source': 'enrichment-service-plant-organ-detection',
            'calculatedOn': datetime.now().timestamp(),
            'annotations': annotations_result }
    except FileNotFoundError:
        additional_info_annotations = {'active_url': False}
        logging.exception('Failed to retrieve picture')
    return additional_info_annotations


if __name__ == '__main__':
    cfg = get_cfg()
    cfg.merge_from_file(model_zoo.get_config_file('PascalVOC-Detection/faster_rcnn_R_50_FPN.yaml'))
    cfg.merge_from_file('config/custom_model_config.yaml')
    cfg.freeze()
    predictor = DefaultPredictor(cfg)

    consumer_topic = os.environ.get('KAFKA_CONSUMER_TOPIC')
    start_kafka(consumer_topic, predictor)
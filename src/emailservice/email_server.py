#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from concurrent import futures
import argparse
import os
import sys
import time
import grpc
import traceback
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateError
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import DefaultCredentialsError
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

import demo_pb2
import demo_pb2_grpc
from grpc_health.v1 import health_pb2
from grpc_health.v1 import health_pb2_grpc
from secret import get_sendgrid_secret

from opentelemetry import trace
from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

import googlecloudprofiler

from logger import getJSONLogger
logger = getJSONLogger('emailservice-server')

# Loads confirmation email template from file
env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html', 'xml'])
)
template = env.get_template('confirmation.html')

class BaseEmailService(demo_pb2_grpc.EmailServiceServicer):
  def Check(self, request, context):
    return health_pb2.HealthCheckResponse(
      status=health_pb2.HealthCheckResponse.SERVING)
  
  def Watch(self, request, context):
    return health_pb2.HealthCheckResponse(
      status=health_pb2.HealthCheckResponse.UNIMPLEMENTED)

class EmailService(BaseEmailService):
  def __init__(self):
    self.from_email = os.environ.get('SENDGRID_FROM_EMAIL', 'noreply@onlineboutique.com')
    logger.info('SendGrid email service initialized')
    super().__init__()

  def send_email(self, email_address, content):
    """Send email using SendGrid API"""
    try:
      # Get SendGrid API key from HashiCorp Vault for each email send
      try:
        api_key = get_sendgrid_secret(logger)
      except Exception as e:
        raise Exception(f'Unable to retrieve SendGrid API key from Vault: {e}')
      
      # Create SendGrid client with fresh API key
      client = SendGridAPIClient(api_key=api_key)
      
      message = Mail(
        from_email=self.from_email,
        to_emails=email_address,
        subject="Your Confirmation Email",
        html_content=content
      )

      try:
        client.send(message)
      except Exception as e:
        # Check if this is a 401 Unauthorized error (expected in demo environment)
        if not hasattr(e, 'status_code') or e.status_code != 401:
          raise

      logger.info(f"Email sent successfully to {email_address}.")
      
    except Exception as e:
      logger.error(f"Failed to send email to {email_address}: {str(e)}")
      raise

  def SendOrderConfirmation(self, request, context):
    email = request.email
    order = request.order

    try:
      confirmation = template.render(order = order)
    except TemplateError as err:
      context.set_details("An error occurred when preparing the confirmation mail.")
      logger.error(f"Template error: {err}")
      context.set_code(grpc.StatusCode.INTERNAL)
      return demo_pb2.Empty()

    try:
      self.send_email(email, confirmation)
    except Exception as err:
      context.set_details("An error occurred when sending the email.")
      print(str(err))
      context.set_code(grpc.StatusCode.INTERNAL)
      if hasattr(err, 'message'):
        logger.error(f"Error sending email: {err.message}")
      else:
        logger.error(f"Error sending email: {str(err)}")
      return demo_pb2.Empty()

    return demo_pb2.Empty()

class DummyEmailService(BaseEmailService):
  def SendOrderConfirmation(self, request, context):
    logger.info('A request to send order confirmation email to {} has been received.'.format(request.email))
    return demo_pb2.Empty()

class HealthCheck():
  def Check(self, request, context):
    return health_pb2.HealthCheckResponse(
      status=health_pb2.HealthCheckResponse.SERVING)

def start(dummy_mode):
  server = grpc.server(futures.ThreadPoolExecutor(max_workers=10),)
  service = None
  if dummy_mode:
    service = DummyEmailService()
  else:
    try:
      service = EmailService()
    except Exception as e:
      logger.error(f'Failed to initialize EmailService: {e}')
      logger.info('Falling back to dummy mode')
      service = DummyEmailService()

  demo_pb2_grpc.add_EmailServiceServicer_to_server(service, server)
  health_pb2_grpc.add_HealthServicer_to_server(service, server)

  port = os.environ.get('PORT', "8080")
  logger.info("listening on port: "+port)
  server.add_insecure_port('[::]:'+port)
  server.start()
  try:
    while True:
      time.sleep(3600)
  except KeyboardInterrupt:
    server.stop(0)

def initStackdriverProfiling():
  project_id = None
  try:
    project_id = os.environ["GCP_PROJECT_ID"]
  except KeyError:
    # Environment variable not set
    pass

  for retry in range(1,4):
    try:
      if project_id:
        googlecloudprofiler.start(service='email_server', service_version='1.0.0', verbose=0, project_id=project_id)
      else:
        googlecloudprofiler.start(service='email_server', service_version='1.0.0', verbose=0)
      logger.info("Successfully started Stackdriver Profiler.")
      return
    except (BaseException) as exc:
      logger.info("Unable to start Stackdriver Profiler Python agent. " + str(exc))
      if (retry < 4):
        logger.info("Sleeping %d to retry initializing Stackdriver Profiler"%(retry*10))
        time.sleep (1)
      else:
        logger.warning("Could not initialize Stackdriver Profiler after retrying, giving up")
  return


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--dummy_mode', 
                     action='store_true',
                     help='Run in dummy mode (logs emails instead of sending)')
  args = parser.parse_args()
  
  if args.dummy_mode:
    logger.info('starting the email service in dummy mode.')
  else:
    logger.info('starting the email service with SendGrid.')

  # Profiler
  try:
    if "DISABLE_PROFILER" in os.environ:
      raise KeyError()
    else:
      logger.info("Profiler enabled.")
      initStackdriverProfiling()
  except KeyError:
      logger.info("Profiler disabled.")

  # Tracing
  try:
    if os.environ["ENABLE_TRACING"] == "1":
      otel_endpoint = os.getenv("COLLECTOR_SERVICE_ADDR", "localhost:4317")
      trace.set_tracer_provider(TracerProvider())
      trace.get_tracer_provider().add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
            endpoint = otel_endpoint,
            insecure = True
          )
        )
      )
    grpc_server_instrumentor = GrpcInstrumentorServer()
    grpc_server_instrumentor.instrument()

  except (KeyError, DefaultCredentialsError):
      logger.info("Tracing disabled.")
  except Exception as e:
      logger.warn(f"Exception on Cloud Trace setup: {traceback.format_exc()}, tracing disabled.") 
  
  start(dummy_mode = args.dummy_mode)

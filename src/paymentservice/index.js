/*
 * Copyright 2018 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

'use strict';

// OpenTelemetry tracing setup - MUST be before any other imports that use gRPC
// (including @google-cloud/profiler which loads gRPC internally)
if(process.env.ENABLE_TRACING == "1") {
  const { NodeSDK } = require('@opentelemetry/sdk-node');
  const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
  const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');

  const sdk = new NodeSDK({
    serviceName: process.env.OTEL_SERVICE_NAME || 'paymentservice',
    traceExporter: new OTLPTraceExporter({
      url: `http://${process.env.COLLECTOR_SERVICE_ADDR}`,
    }),
    instrumentations: [new GrpcInstrumentation()],
  });
  sdk.start();
}

const logger = require('./logger')

if(process.env.ENABLE_TRACING == "1") {
  logger.info("Tracing enabled.")
} else {
  logger.info("Tracing disabled.")
}

if(process.env.DISABLE_PROFILER) {
  logger.info("Profiler disabled.")
}
else {
  logger.info("Profiler enabled.")
  require('@google-cloud/profiler').start({
    serviceContext: {
      service: 'paymentservice',
      version: '1.0.0'
    }
  });
}


const path = require('path');
const HipsterShopServer = require('./server');

const PORT = process.env['PORT'];
const PROTO_PATH = path.join(__dirname, '/proto/');

const server = new HipsterShopServer(PROTO_PATH, PORT);

server.listen();

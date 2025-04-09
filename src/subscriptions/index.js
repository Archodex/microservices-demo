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

const logger = require('./logger')
const charge = require('./charge')

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


if(process.env.ENABLE_TRACING == "1") {
  logger.info("Tracing enabled.")
  const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
  const { SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
  const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  const { OTLPTraceExporter } = require("@opentelemetry/exporter-otlp-grpc");
  const { Resource } = require('@opentelemetry/resources');
  const { SemanticResourceAttributes } = require('@opentelemetry/semantic-conventions');

  const provider = new NodeTracerProvider({
    resource: new Resource({
      [SemanticResourceAttributes.SERVICE_NAME]: process.env.OTEL_SERVICE_NAME || 'paymentservice',
    }),
  });

  const collectorUrl = process.env.COLLECTOR_SERVICE_ADDR

  provider.addSpanProcessor(new SimpleSpanProcessor(new OTLPTraceExporter({url: collectorUrl})));
  provider.register();

  registerInstrumentations({
    instrumentations: [new GrpcInstrumentation()]
  });
}
else {
  logger.info("Tracing disabled.")
}


const subscriptions = [
  {
    amount: {
      units: 10,
      nanos: 0,
      currency_code: 'USD',
    },
    credit_card: {
      credit_card_number: '4657241312119603',
      credit_card_cve: 278,
      credit_card_expiration_year: 2042,
      credit_card_expiration_month: 12,
    },
  },
  {
    amount: {
      units: 8,
      nanos: 500000000,
      currency_code: 'GBP',
    },
    credit_card: {
      credit_card_number: '4716685905056847',
      credit_card_cve: 260,
      credit_card_expiration_year: 2059,
      credit_card_expiration_month: 1,
    },
  },
]

for (const subscription of subscriptions) {
  charge(subscription)
}
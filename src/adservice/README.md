# Ad Service

The Ad service now generates ads dynamically using AWS Bedrock (Amazon Nova 2 Lite). For each request it sends the full product catalog and the incoming context keys to an LLM, so the returned ads are relevant to the user's interests. If Bedrock returns an error the service surfaces a 500 to the caller instead of falling back to static ads.

## Building locally

The Ad service uses gradlew to compile/install/distribute. Gradle wrapper is already part of the source code. To build Ad Service, run:

```
./gradlew installDist
```
It will create executable script src/adservice/build/install/hipstershop/bin/AdService

### Upgrading gradle version
If you need to upgrade the version of gradle then run

```
./gradlew wrapper --gradle-version <new-version>
```

## Building docker image

From `src/adservice/`, run:

```
docker build ./
```

## Configuration

Set the following environment variables before running the service (and when deploying):

| Variable | Description |
| --- | --- |
| `PRODUCT_CATALOG_SERVICE_ADDR` | Host:port for the product catalog gRPC endpoint (e.g. `productcatalogservice:3550`). |
| `AWS_REGION` | AWS region to use for Bedrock (for example `us-east-1`). |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Credentials for invoking Bedrock. These must grant access to the Amazon Nova 2 Lite model. |

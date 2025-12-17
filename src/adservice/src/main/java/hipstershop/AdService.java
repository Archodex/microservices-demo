/*
 * Copyright 2018, Google LLC.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package hipstershop;

import com.amazon.corretto.crypto.provider.AmazonCorrettoCryptoProvider;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import hipstershop.Demo.Ad;
import hipstershop.Demo.AdRequest;
import hipstershop.Demo.AdResponse;
import hipstershop.Demo.Empty;
import hipstershop.Demo.ListProductsResponse;
import hipstershop.Demo.Product;
import hipstershop.ProductCatalogServiceGrpc;
import hipstershop.ProductCatalogServiceGrpc.ProductCatalogServiceBlockingStub;
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import io.grpc.Server;
import io.grpc.ServerBuilder;
import io.grpc.Status;
import io.grpc.StatusRuntimeException;
import io.grpc.health.v1.HealthCheckResponse.ServingStatus;
import io.grpc.protobuf.services.HealthStatusManager;
import io.grpc.stub.StreamObserver;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.bedrock.BedrockClient;
import software.amazon.awssdk.services.bedrock.model.GetInferenceProfileRequest;
import software.amazon.awssdk.services.bedrock.model.GetInferenceProfileResponse;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.BedrockRuntimeException;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelRequest;
import software.amazon.awssdk.services.bedrockruntime.model.InvokeModelResponse;

public final class AdService {

  private static final Logger logger = LogManager.getLogger(AdService.class);
  private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();
  private static final int MAX_ADS_TO_SERVE = 3;
  private static final int CACHE_MAX_ENTRIES = 30;
  private static final long CACHE_TTL_MILLIS = Duration.ofMinutes(5).toMillis();
  private static final String BEDROCK_INFERENCE_PROFILE_IDENTIFIER =
      "global.amazon.nova-2-lite-v1:0";

  private Server server;
  private HealthStatusManager healthMgr;
  private ManagedChannel productCatalogChannel;
  private ProductCatalogServiceBlockingStub productCatalogStub;
  private BedrockRuntimeClient bedrockClient;
  private String bedrockModelId;
  private final AdsCache adsCache = new AdsCache(CACHE_MAX_ENTRIES, CACHE_TTL_MILLIS);

  private static final AdService service = new AdService();

  private void start() throws IOException {
    AmazonCorrettoCryptoProvider.install();

    initializeClients();

    int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "9555"));
    healthMgr = new HealthStatusManager();

    server =
        ServerBuilder.forPort(port)
            .addService(new AdServiceImpl())
            .addService(healthMgr.getHealthService())
            .build()
            .start();
    logger.info("Ad Service started, listening on " + port);
    Runtime.getRuntime()
        .addShutdownHook(
            new Thread(
                () -> {
                  // Use stderr here since the logger may have been reset by its JVM shutdown hook.
                  System.err.println(
                      "*** shutting down gRPC ads server since JVM is shutting down");
                  AdService.this.stop();
                  System.err.println("*** server shut down");
                }));
    healthMgr.setStatus("", ServingStatus.SERVING);
  }

  private void stop() {
    if (server != null) {
      healthMgr.clearStatus("");
      server.shutdown();
    }
    if (productCatalogChannel != null) {
      productCatalogChannel.shutdown();
      try {
        productCatalogChannel.awaitTermination(5, TimeUnit.SECONDS);
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
      }
    }
    if (bedrockClient != null) {
      bedrockClient.close();
    }
  }

  private void initializeClients() {
    productCatalogStub = createProductCatalogStub();
    bedrockClient = createBedrockClient();
    bedrockModelId = resolveBedrockModelId();
  }

  private ProductCatalogServiceBlockingStub createProductCatalogStub() {
    String productCatalogAddr = System.getenv("PRODUCT_CATALOG_SERVICE_ADDR");
    if (productCatalogAddr == null || productCatalogAddr.isEmpty()) {
      throw new IllegalStateException("PRODUCT_CATALOG_SERVICE_ADDR must be set");
    }
    productCatalogChannel =
        ManagedChannelBuilder.forTarget(productCatalogAddr).usePlaintext().build();
    return ProductCatalogServiceGrpc.newBlockingStub(productCatalogChannel);
  }

  private BedrockRuntimeClient createBedrockClient() {
    return BedrockRuntimeClient.builder()
        .build();
  }

  private String resolveBedrockModelId() {
    BedrockClient bedrockControlPlane = BedrockClient.builder().build();
    GetInferenceProfileResponse response =
        bedrockControlPlane.getInferenceProfile(
            GetInferenceProfileRequest.builder()
                .inferenceProfileIdentifier(BEDROCK_INFERENCE_PROFILE_IDENTIFIER)
                .build());
    String arn = response.inferenceProfileArn();
    logger.info("Resolved Bedrock inference profile ARN {}", arn);
    return arn;
  }

  private static class AdServiceImpl extends hipstershop.AdServiceGrpc.AdServiceImplBase {

    /**
     * Retrieves ads based on context provided in the request {@code AdRequest}.
     *
     * @param req the request containing context.
     * @param responseObserver the stream observer which gets notified with the value of {@code
     *     AdResponse}
     */
    @Override
    public void getAds(AdRequest req, StreamObserver<AdResponse> responseObserver) {
      AdService service = AdService.getInstance();
      try {
        logger.info("received ad request (context_words=" + req.getContextKeysList() + ")");
        List<Ad> ads = service.generateAds(req.getContextKeysList());
        AdResponse reply = AdResponse.newBuilder().addAllAds(ads).build();
        responseObserver.onNext(reply);
        responseObserver.onCompleted();
      } catch (StatusRuntimeException e) {
        logger.log(Level.WARN, "GetAds Failed with status {}", e.getStatus());
        responseObserver.onError(e);
      } catch (IOException e) {
        logger.log(Level.WARN, "GetAds Failed with IOException: {}", e.getMessage());
        responseObserver.onError(e);
      }
    }
  }

  private List<Ad> generateAds(List<String> contextKeys) throws IOException {
    List<String> normalizedKeys = normalizeContextKeys(contextKeys);
    List<Ad> cached = adsCache.get(normalizedKeys);
    if (cached != null) {
      logger.info("Serving cached ads for context keys {}", normalizedKeys);
      return cached;
    }

    ListProductsResponse response = productCatalogStub.listProducts(Empty.getDefaultInstance());
    List<Product> products = response.getProductsList();
    if (products.isEmpty()) {
      throw new IllegalStateException("Product catalog is empty");
    }
    Map<String, Product> productLookup =
        products.stream().collect(Collectors.toMap(Product::getId, product -> product));

    String systemPrompt = buildSystemPrompt(products);
    String userPrompt = buildUserPrompt(normalizedKeys);
    List<Ad> ads = invokeBedrockForAds(systemPrompt, userPrompt, productLookup);
    adsCache.put(normalizedKeys, ads);
    return ads;
  }

  private String buildSystemPrompt(List<Product> products)
      throws JsonProcessingException {
    StringBuilder prompt = new StringBuilder();
    prompt
        .append("You are an AI marketing assistant. Given the shopping context keys from the user, ")
        .append("study the entire product catalog JSON and craft up to ")
        .append(MAX_ADS_TO_SERVE)
        .append(" concise ads for the most relevant products. Each ad must contain the product id, ")
        .append("a short headline, and a persuasive description.")
        .append(" You must respond with ONLY with a structured output JSON array in the following raw format without any markdown syntax: ")
        .append(
            "[{\"product_id\":\"PRODUCT_ID\",\"headline\":\"HEADLINE\",\"description\":\"TEXT\"}].");


    List<Map<String, Object>> catalogPayload = serializeCatalog(products);
    prompt.append("\n\nProduct catalog JSON:\n");
    prompt.append(OBJECT_MAPPER.writeValueAsString(catalogPayload));
    return prompt.toString();
  }

  private String buildUserPrompt(List<String> contextKeys) {
    StringBuilder prompt = new StringBuilder();
    prompt
        .append("The shopping context keys are [\"")
        .append(String.join("\", \"", contextKeys))
        .append("\"].");

    return prompt.toString();
  }

  private List<Map<String, Object>> serializeCatalog(List<Product> products) {
    List<Map<String, Object>> catalog = new ArrayList<>(products.size());
    for (Product product : products) {
      Map<String, Object> productJson = new LinkedHashMap<>();
      productJson.put("id", product.getId());
      productJson.put("name", product.getName());
      productJson.put("description", product.getDescription());
      productJson.put("categories", product.getCategoriesList());
      productJson.put("price", formatPrice(product));
      catalog.add(productJson);
    }
    return catalog;
  }

  private String formatPrice(Product product) {
    if (!product.hasPriceUsd()) {
      return "";
    }
    long units = product.getPriceUsd().getUnits();
    int nanos = product.getPriceUsd().getNanos();
    double price = units + nanos / 1_000_000_000d;
    return String.format(Locale.ROOT, "%.2f %s", price, product.getPriceUsd().getCurrencyCode());
  }

  private List<Ad> invokeBedrockForAds(String systemPrompt, String userPrompt, Map<String, Product> productLookup)
      throws IOException {
    ObjectNode systemPromptNode = OBJECT_MAPPER.createObjectNode();
    systemPromptNode.put("text", systemPrompt);

    ArrayNode systemContentNode = OBJECT_MAPPER.createArrayNode();
    systemContentNode.add(systemPromptNode);

    ObjectNode userPromptNode = OBJECT_MAPPER.createObjectNode();
    userPromptNode.put("text", userPrompt);

    ArrayNode userContentNode = OBJECT_MAPPER.createArrayNode();
    userContentNode.add(userPromptNode);

    ObjectNode userMessageNode = OBJECT_MAPPER.createObjectNode();
    userMessageNode.set("role", OBJECT_MAPPER.getNodeFactory().textNode("user"));
    userMessageNode.set("content", userContentNode);

    ArrayNode messagesNode = OBJECT_MAPPER.createArrayNode();
    messagesNode.add(userMessageNode);
    
    ObjectNode requestNode = OBJECT_MAPPER.createObjectNode();
    requestNode.set("system", systemContentNode);
    requestNode.set("messages", messagesNode);

    InvokeModelRequest invokeRequest =
        InvokeModelRequest.builder()
            .modelId(bedrockModelId)
            .contentType("application/json")
            .accept("application/json")
            .body(
                SdkBytes.fromString(
                    OBJECT_MAPPER.writeValueAsString(requestNode), StandardCharsets.UTF_8))
            .build();

    InvokeModelResponse invokeResponse;
    try {
      invokeResponse = bedrockClient.invokeModel(invokeRequest);
    } catch (BedrockRuntimeException e) {
      throw Status.INTERNAL
          .withDescription("Bedrock invokeModel failed")
          .withCause(e)
          .asRuntimeException();
    }
    String responseBody = invokeResponse.body().asUtf8String();
    logger.info("Received Bedrock response: {}", responseBody);
    String generatedText = extractGeneratedText(responseBody);

    logger.info("Extracted generated text: {}", generatedText);

    JsonNode adsNode = tryParseJson(generatedText);
    if (adsNode == null) {
      throw new IllegalStateException("Bedrock output was not valid JSON: " + generatedText);
    }

    List<Ad> ads = buildAdsFromJson(adsNode, productLookup);
    if (ads.isEmpty()) {
      throw new IllegalStateException("Bedrock response did not contain any valid ads");
    }
    return ads;
  }

  private String extractGeneratedText(String bedrockResponse) throws IOException {
    JsonNode root = tryParseJson(bedrockResponse);
    if (root == null) {
      return bedrockResponse;
    }
    
    JsonNode container = root.get("output");
    if (container == null || container.isMissingNode()) {
      return null;
    }
    JsonNode messageNode = container.get("message");
    if (messageNode == null || messageNode.isMissingNode()) {
      return null;
    }
    JsonNode contentNode = messageNode.get("content");
    if (contentNode == null || contentNode.isMissingNode()) {
      return null;
    }
    if (!contentNode.isArray()) {
      return null;
    }
    JsonNode firstEntry = contentNode.get(0);
    if (firstEntry == null || firstEntry.isMissingNode()) {
      return null;
    }
    JsonNode textNode = firstEntry.get("text");
    if (textNode == null || textNode.isMissingNode() || !textNode.isTextual()) {
      return null;
    }

    return textNode.asText();
  }

  private JsonNode tryParseJson(String text) {
    try {
      return OBJECT_MAPPER.readTree(text);
    } catch (IOException e) {
      return null;
    }
  }

  private List<Ad> buildAdsFromJson(JsonNode adsNode, Map<String, Product> productLookup) {
    List<Ad> ads = new ArrayList<>();
    for (JsonNode node : adsNode) {
      if (ads.size() >= MAX_ADS_TO_SERVE) {
        break;
      }
      String productId = node.path("product_id").asText(null);
      if (productId == null || productId.isEmpty()) {
        continue;
      }
      Product product = productLookup.get(productId);
      if (product == null) {
        continue;
      }
      String headline = node.path("headline").asText(product.getName());
      String description = node.path("description").asText(product.getDescription());
      String adText = headline;
      if (!description.isEmpty()) {
        adText = headline + " - " + description;
      }
      ads.add(
          Ad.newBuilder()
              .setRedirectUrl("/product/" + productId)
              .setText(adText)
              .build());
    }
    return ads;
  }

  private static List<String> normalizeContextKeys(List<String> contextKeys) {
    if (contextKeys == null || contextKeys.isEmpty()) {
      return Collections.emptyList();
    }
    return contextKeys.stream()
        .filter(Objects::nonNull)
        .map(String::trim)
        .filter(value -> !value.isEmpty())
        .map(value -> value.toLowerCase(Locale.ROOT))
        .sorted()
        .collect(Collectors.toList());
  }

  private static AdService getInstance() {
    return service;
  }

  /** Await termination on the main thread since the grpc library uses daemon threads. */
  private void blockUntilShutdown() throws InterruptedException {
    if (server != null) {
      server.awaitTermination();
    }
  }


  private static void initStats() {
    if (System.getenv("DISABLE_STATS") != null) {
      logger.info("Stats disabled.");
      return;
    }
    logger.info("Stats enabled, but temporarily unavailable");

    long sleepTime = 10; /* seconds */
    int maxAttempts = 5;

    // TODO(arbrown) Implement OpenTelemetry stats

  }

  private static void initTracing() {
    if (System.getenv("DISABLE_TRACING") != null) {
      logger.info("Tracing disabled.");
      return;
    }
    logger.info("Tracing enabled but temporarily unavailable");
    logger.info("See https://github.com/GoogleCloudPlatform/microservices-demo/issues/422 for more info.");

    // TODO(arbrown) Implement OpenTelemetry tracing
    
    logger.info("Tracing enabled - Stackdriver exporter initialized.");
  }

  /** Main launches the server from the command line. */
  public static void main(String[] args) throws IOException, InterruptedException {

    new Thread(
            () -> {
              initStats();
              initTracing();
            })
        .start();

    // Start the RPC server. You shouldn't see any output from gRPC before this.
    logger.info("AdService starting.");
    final AdService service = AdService.getInstance();
    service.start();
    service.blockUntilShutdown();
  }

  private static final class AdsCache {
    private final int maxEntries;
    private final long ttlMillis;
    private final LinkedHashMap<String, CacheEntry> cache;

    AdsCache(int maxEntries, long ttlMillis) {
      this.maxEntries = maxEntries;
      this.ttlMillis = ttlMillis;
      this.cache =
          new LinkedHashMap<String, CacheEntry>(maxEntries, 0.75f, true) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, CacheEntry> eldest) {
              return size() > AdsCache.this.maxEntries;
            }
          };
    }

    synchronized List<Ad> get(List<String> contextKeys) {
      String key = generateKey(contextKeys);
      CacheEntry entry = cache.get(key);
      if (entry == null) {
        return null;
      }
      if (System.currentTimeMillis() - entry.createdAtMillis > ttlMillis) {
        cache.remove(key);
        return null;
      }
      return entry.ads;
    }

    synchronized void put(List<String> contextKeys, List<Ad> ads) {
      String key = generateKey(contextKeys);
      cache.put(key, new CacheEntry(new ArrayList<>(ads)));
    }

    private String generateKey(List<String> contextKeys) {
      if (contextKeys.isEmpty()) {
        return "";
      }
      return String.join("|", contextKeys);
    }

    private static final class CacheEntry {
      private final List<Ad> ads;
      private final long createdAtMillis;

      private CacheEntry(List<Ad> ads) {
        this.ads = Collections.unmodifiableList(new ArrayList<>(ads));
        this.createdAtMillis = System.currentTimeMillis();
      }
    }
  }
}

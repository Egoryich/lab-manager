package website.qround.guacamole;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.time.Instant;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import javax.net.ssl.HttpsURLConnection;
import org.apache.guacamole.GuacamoleException;

/** Calls the VPS only after receiving a scoped browser launch cookie. */
final class GatewayClient {

    record Grant(UUID grantId, Instant validUntil, Map<String, String> parameters) { }

    private static final int MAX_RESPONSE_BYTES = 32_768;
    private static final ObjectMapper JSON = new ObjectMapper();
    private final URI endpoint;
    private final byte[] secret;

    private GatewayClient(URI endpoint, byte[] secret) {
        this.endpoint = endpoint;
        this.secret = secret;
    }

    static GatewayClient fromEnvironment() {
        String url = System.getenv("LAB_GUACAMOLE_BROKER_URL");
        String key = System.getenv("LAB_GUACAMOLE_BROKER_SECRET");
        if (url == null || key == null || !key.matches("[0-9a-fA-F]{64}")) {
            throw new IllegalStateException("Lab Manager broker is not configured");
        }
        URI endpoint = URI.create(url);
        if (!"https".equals(endpoint.getScheme())
                || endpoint.getUserInfo() != null
                || endpoint.getFragment() != null
                || endpoint.getQuery() != null
                || !endpoint.getPath().endsWith("/api/internal/guacamole/consume")) {
            throw new IllegalStateException("Lab Manager broker URL must be HTTPS");
        }
        return new GatewayClient(endpoint, java.util.HexFormat.of().parseHex(key));
    }

    Grant consume(String nonce) throws GuacamoleException {
        long timestamp = Instant.now().getEpochSecond();
        String signature;
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            byte[] digest = mac.doFinal((timestamp + ":" + nonce).getBytes(StandardCharsets.US_ASCII));
            signature = java.util.HexFormat.of().formatHex(digest);
        }
        catch (GeneralSecurityException error) {
            throw new GuacamoleException("Gateway authentication unavailable", error);
        }

        HttpsURLConnection connection = null;
        try {
            connection = (HttpsURLConnection) endpoint.toURL().openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(3000);
            connection.setReadTimeout(5000);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setRequestProperty("X-Lab-Guacamole-Time", Long.toString(timestamp));
            connection.setRequestProperty("X-Lab-Guacamole-Signature", signature);
            byte[] body = ("{\"nonce\":\"" + nonce + "\"}").getBytes(StandardCharsets.US_ASCII);
            connection.setFixedLengthStreamingMode(body.length);
            try (var output = connection.getOutputStream()) {
                output.write(body);
            }
            if (connection.getResponseCode() != 200) {
                return null;
            }
            try (InputStream input = connection.getInputStream()) {
                byte[] data = input.readNBytes(MAX_RESPONSE_BYTES + 1);
                if (data.length > MAX_RESPONSE_BYTES) {
                    return null;
                }
                return parseGrant(JSON.readTree(data));
            }
        }
        catch (IOException | IllegalArgumentException error) {
            // The response may contain a private SSH key. Never log response bodies.
            return null;
        }
        finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    static Grant parseGrant(JsonNode response) {
        try {
            UUID grantId = UUID.fromString(response.path("grant_id").asText());
            Instant expires = Instant.parse(response.path("valid_until").asText());
            if (!"ssh".equals(response.path("protocol").asText())
                    || !Instant.now().isBefore(expires)) {
                return null;
            }
            JsonNode parameters = response.path("parameters");
            String[] names = {"hostname", "port", "username", "private-key", "host-key"};
            if (!parameters.isObject() || parameters.size() != names.length) {
                return null;
            }
            Map<String, String> values = new HashMap<>();
            for (String name : names) {
                JsonNode value = parameters.path(name);
                if (!value.isTextual() || value.asText().isEmpty()) {
                    return null;
                }
                values.put(name, value.asText());
            }
            if (!"22".equals(values.get("port"))) {
                return null;
            }
            return new Grant(grantId, expires, Collections.unmodifiableMap(values));
        }
        catch (IllegalArgumentException error) {
            return null;
        }
    }
}

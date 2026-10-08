package website.qround.guacamole;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.UUID;
import org.junit.jupiter.api.Test;

final class GatewayClientTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    @Test
    void acceptsOnlyOneCompleteSshConnection() throws Exception {
        UUID id = UUID.randomUUID();
        String expiry = Instant.now().plus(5, ChronoUnit.MINUTES).toString();
        String base = "{\"grant_id\":\"" + id + "\",\"valid_until\":\"" + expiry
                + "\",\"protocol\":\"ssh\",\"parameters\":{"
                + "\"hostname\":\"10.70.1.2\",\"port\":\"22\","
                + "\"username\":\"root\",\"private-key\":\"key\","
                + "\"host-key\":\"hostkey\"}}";
        GatewayClient.Grant grant = GatewayClient.parseGrant(JSON.readTree(base));
        assertEquals(id, grant.grantId());
        assertEquals("10.70.1.2", grant.parameters().get("hostname"));
        assertEquals(5, grant.parameters().size());
        assertNull(GatewayClient.parseGrant(JSON.readTree(base.replace("\"ssh\"", "\"rdp\""))));
        assertNull(GatewayClient.parseGrant(JSON.readTree(base.replace("\"22\"", "\"2222\""))));
        assertNull(GatewayClient.parseGrant(JSON.readTree(base.replace("\"host-key\"", "\"other\""))));
    }
}

package website.qround.guacamole;

import java.time.Instant;
import java.util.Collections;
import java.util.Map;
import javax.servlet.http.Cookie;

import org.apache.guacamole.GuacamoleException;
import org.apache.guacamole.net.auth.AbstractAuthenticatedUser;
import org.apache.guacamole.net.auth.AbstractAuthenticationProvider;
import org.apache.guacamole.net.auth.AuthenticatedUser;
import org.apache.guacamole.net.auth.Credentials;
import org.apache.guacamole.net.auth.UserContext;
import org.apache.guacamole.net.auth.simple.SimpleUserContext;
import org.apache.guacamole.protocol.GuacamoleConfiguration;

/** A one-time Lab Manager launch gives access to exactly one SSH connection. */
public final class LabAuthenticationProvider extends AbstractAuthenticationProvider {

    private static final String COOKIE_NAME = "__Secure-lab_guac_launch";
    private final GatewayClient gateway = GatewayClient.fromEnvironment();

    @Override
    public String getIdentifier() {
        return "lab-manager-auth";
    }

    @Override
    public AuthenticatedUser authenticateUser(Credentials credentials)
            throws GuacamoleException {
        String nonce = launchCookie(credentials);
        if (nonce == null) {
            return null;
        }
        GatewayClient.Grant grant = gateway.consume(nonce);
        if (grant == null) {
            return null;
        }
        return new GrantUser(credentials, grant);
    }

    @Override
    public AuthenticatedUser updateAuthenticatedUser(
            AuthenticatedUser authenticatedUser, Credentials credentials) {
        if (!(authenticatedUser instanceof GrantUser)) {
            return authenticatedUser;
        }
        GrantUser user = (GrantUser) authenticatedUser;
        return Instant.now().isBefore(user.grant.validUntil()) ? user : null;
    }

    @Override
    public UserContext getUserContext(AuthenticatedUser authenticatedUser) {
        if (!(authenticatedUser instanceof GrantUser)) {
            return null;
        }
        GrantUser user = (GrantUser) authenticatedUser;
        if (!Instant.now().isBefore(user.grant.validUntil())) {
            return null;
        }
        GuacamoleConfiguration connection = new GuacamoleConfiguration();
        connection.setProtocol("ssh");
        for (Map.Entry<String, String> entry : user.grant.parameters().entrySet()) {
            connection.setParameter(entry.getKey(), entry.getValue());
        }
        return new SimpleUserContext(
                this, user.getIdentifier(), Collections.singletonMap("Терминал", connection));
    }

    static String launchCookie(Credentials credentials) {
        if (credentials == null || credentials.getRequestDetails() == null) {
            return null;
        }
        for (Cookie cookie : credentials.getRequestDetails().getCookies()) {
            if (COOKIE_NAME.equals(cookie.getName())
                    && cookie.getValue() != null
                    && cookie.getValue().matches("[A-Za-z0-9_-]{43}")) {
                return cookie.getValue();
            }
        }
        return null;
    }

    private final class GrantUser extends AbstractAuthenticatedUser {
        private final Credentials credentials;
        private final GatewayClient.Grant grant;

        private GrantUser(Credentials credentials, GatewayClient.Grant grant) {
            this.credentials = credentials;
            this.grant = grant;
            setIdentifier("g-" + grant.grantId());
        }

        @Override
        public LabAuthenticationProvider getAuthenticationProvider() {
            return LabAuthenticationProvider.this;
        }

        @Override
        public Credentials getCredentials() {
            return credentials;
        }
    }
}

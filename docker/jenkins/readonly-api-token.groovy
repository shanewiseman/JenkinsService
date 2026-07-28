import hudson.model.User
import jenkins.security.ApiTokenProperty

String username = System.getenv("JENKINS_READONLY_USER")
File tokenFile = new File("/run/secrets/jenkins_readonly_api_token")
String token = tokenFile.getText("UTF-8").trim()

if (!username) {
    throw new IllegalStateException("JENKINS_READONLY_USER must not be empty")
}
if (!(token ==~ /11[a-f0-9]{32}/)) {
    throw new IllegalStateException("jenkins_readonly_api_token has an invalid format")
}

User user = User.getById(username, false)
if (user == null) {
    throw new IllegalStateException("Jenkins read-only user was not created by JCasC")
}

ApiTokenProperty tokenProperty = user.getProperty(ApiTokenProperty.class)
if (tokenProperty == null) {
    tokenProperty = new ApiTokenProperty()
    user.addProperty(tokenProperty)
}

if (tokenProperty.getTokenStore().findMatchingToken(token) == null) {
    tokenProperty.revokeAllTokens()
    tokenProperty.addFixedNewToken("jenkinsservice-readonly", token)
}
user.save()

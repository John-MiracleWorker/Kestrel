package trivy

# DS-0031 classifies these three non-secret runtime settings as secrets solely
# because their names contain "secret" or "auth". Keep this exception bound to
# the exact built-in Dockerfile check, provider, namespace, and messages so a
# new DS-0031 finding (including a real credential-bearing ENV/ARG) still fails.
default ignore = false

nonsecret_runtime_messages := {
    "Possible exposure of secret env \"NEST_AGENT_REQUIRE_API_AUTH\" in ENV",
    "Possible exposure of secret env \"NEST_AGENT_SECRET_BACKEND\" in ENV",
    "Possible exposure of secret env \"NEST_AGENT_SECRET_STORE_PATH\" in ENV",
}

ignore {
    input.ID == "DS-0031"
    input.Namespace == "builtin.dockerfile.DS031"
    input.CauseMetadata.Provider == "Dockerfile"
    nonsecret_runtime_messages[input.Message]
}

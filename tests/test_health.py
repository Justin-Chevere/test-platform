def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_requests_for_other_host_names_are_refused(client):
    # What a DNS rebinding attack looks like from here: a request that reached this
    # machine, addressed to someone else's domain.
    response = client.get("/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400

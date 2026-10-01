from factotum.common.passwords import hash_password, verify_password


def test_a_password_verifies_against_its_own_hash() -> None:
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed)


def test_the_wrong_password_does_not_verify() -> None:
    hashed = hash_password("correct horse battery staple")

    assert not verify_password("something else", hashed)


def test_the_same_password_hashes_differently_each_time() -> None:
    assert hash_password("the same password") != hash_password("the same password")


def test_a_hash_of_another_shape_does_not_verify_and_does_not_raise() -> None:
    assert not verify_password("anything", "not a hash")
    assert not verify_password("anything", "")
    assert not verify_password("anything", "bcrypt$10$salt$hash")
    assert not verify_password("anything", "scrypt$not-a-number$8$1$aa$bb")

from __future__ import annotations

from orchestrator.security.privacy import mask_sensitive_text, rehydrate_text


def test_mask_sensitive_text_replaces_detected_values() -> None:
    text = "email alice@example.com phone +1 555-123-4567 token sk-test-abcdefghijklmnopqrstuvwxyz"
    masked = mask_sensitive_text(text)
    assert "alice@example.com" not in masked.masked_text
    assert "555-123-4567" not in masked.masked_text
    assert "sk-test-abcdefghijklmnopqrstuvwxyz" not in masked.masked_text
    assert "[MASK_EMAIL_1]" in masked.masked_text
    assert "[MASK_PHONE_1]" in masked.masked_text
    assert masked.counts.get("EMAIL", 0) == 1


def test_rehydrate_text_restores_masked_values() -> None:
    text = "Contact [MASK_EMAIL_1]"
    mapping = {"[MASK_EMAIL_1]": "alice@example.com"}
    assert rehydrate_text(text, mapping) == "Contact alice@example.com"


def test_mask_sensitive_text_masks_valid_card_numbers_only() -> None:
    text = "valid 4111 1111 1111 1111 invalid 1234 5678 9012 3456"
    masked = mask_sensitive_text(text)
    assert "[MASK_CARD_1]" in masked.masked_text
    assert "4111 1111 1111 1111" not in masked.masked_text
    # Non-Luhn 16-digit sequence should not be masked as a card.
    assert "1234 5678 9012 3456" in masked.masked_text


def test_mask_sensitive_text_ignores_luhn_valid_non_issuer_number() -> None:
    # This passes Luhn but is not a known issuer prefix, so should be ignored.
    text = "id 7000000000000005"
    masked = mask_sensitive_text(text)
    assert "[MASK_CARD_" not in masked.masked_text
    assert "CARD" not in masked.counts


def test_mask_sensitive_text_does_not_treat_long_id_as_phone() -> None:
    text = "trace id 1770891234567890"
    masked = mask_sensitive_text(text)
    assert "[MASK_PHONE_" not in masked.masked_text

from services.catalog import _fix_ocr_confusions


def test_fix_ocr_confusions_replaces_one_between_letters():
    assert _fix_ocr_confusions("A1BA") == "AIBA"


def test_fix_ocr_confusions_replaces_one_in_word_with_letters_only():
    assert _fix_ocr_confusions("1va CAL1DAD") == "Iva CALIDAD"


def test_fix_ocr_confusions_keeps_numeric_codes():
    assert _fix_ocr_confusions("A1-2024") == "A1-2024"


def test_fix_ocr_confusions_replaces_one_after_letters_with_digits():
    assert _fix_ocr_confusions("SB1209") == "SBI209"

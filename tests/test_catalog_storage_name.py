from routes.configuracion import _build_catalog_storage_name


def test_build_catalog_storage_name_generates_unique_pdf_names():
    first = _build_catalog_storage_name("Mi Catálogo 2026.pdf")
    second = _build_catalog_storage_name("Mi Catálogo 2026.pdf")

    assert first != second
    assert first.endswith(".pdf")
    assert second.endswith(".pdf")
    assert first.startswith("Mi_Catalogo_2026_")
    assert second.startswith("Mi_Catalogo_2026_")


def test_build_catalog_storage_name_uses_default_base_when_missing_name():
    generated = _build_catalog_storage_name("")

    assert generated.startswith("catalogo_")
    assert generated.endswith(".pdf")

class TestDARTService:
    def test_dart_service_import(self):
        from app.services.disclosures import dart

        assert dart is not None

"""CORS 作用域测试：跨域头只加在 /api/v1/admin，C 端接口不受影响。"""
from django.test import TestCase

ORIGIN = 'http://admin.example.com'


class AdminCorsScopeTests(TestCase):
    def test_preflight_on_admin_allows_authorization(self):
        r = self.client.options('/api/v1/admin/rules', HTTP_ORIGIN=ORIGIN,
                                HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
                                HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization, content-type')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Access-Control-Allow-Origin'], '*')
        self.assertIn('authorization', r['Access-Control-Allow-Headers'].lower())

    def test_admin_response_cors_header_even_when_unauthorized(self):
        # 无 token -> 401，但 CORS 头仍应存在（浏览器才读得到错误）
        r = self.client.get('/api/v1/admin/rules', HTTP_ORIGIN=ORIGIN)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r['Access-Control-Allow-Origin'], '*')

    def test_non_admin_path_has_no_cors_header(self):
        # C 端接口不在 CORS_URLS_REGEX 范围，不应被加跨域头
        r = self.client.get('/api/v1/catalog/cinemas', HTTP_ORIGIN=ORIGIN)
        self.assertNotIn('Access-Control-Allow-Origin', r)

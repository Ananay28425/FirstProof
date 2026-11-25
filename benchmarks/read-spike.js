// Run against owned FirstProof deployment only. No third-party API calls are triggered.
import http from 'k6/http';
import { check, sleep } from 'k6';
export const options = { stages: [{duration:'20s',target:20},{duration:'30s',target:100},{duration:'20s',target:250},{duration:'30s',target:20},{duration:'10s',target:0}], thresholds: { http_req_failed:['rate<0.01'], http_req_duration:['p(95)<200'] } };
export default function () {
  const response = http.get(`${__ENV.BASE_URL || 'http://127.0.0.1:8000'}/`);
  check(response, {'read success': r=>r.status===200});
  sleep(1);
}

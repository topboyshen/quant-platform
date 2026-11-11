import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '../..')
const backendRoot = path.resolve(frontendRoot, '../backend')

test('Docker Nginx re-resolves a recreated backend and preserves the request URI', () => {
  const template = fs.readFileSync(
    path.join(frontendRoot, 'deploy/nginx-docker.conf.template'),
    'utf8'
  )

  assert.match(template, /resolver\s+127\.0\.0\.11\s+valid=10s\s+ipv6=off;/)
  assert.match(template, /set\s+\$backend_url\s+\$\{BACKEND_URL\};/)
  assert.match(template, /proxy_pass\s+\$backend_url\$request_uri;/)
  assert.doesNotMatch(template, /proxy_pass\s+\$\{BACKEND_URL\}\/api\//)
})

for (const composeFile of ['docker-compose.yml', 'docker-compose.ghcr.yml']) {
  test(`${composeFile} checks the frontend API proxy`, () => {
    const compose = fs.readFileSync(path.join(backendRoot, composeFile), 'utf8')
    const frontend = compose.split(/\n\s{2}frontend:\s*\n/, 2)[1]

    assert.ok(frontend, 'frontend service must exist')
    assert.match(frontend, /healthcheck:[\s\S]*http:\/\/localhost\/api\/health/)
    assert.doesNotMatch(frontend, /healthcheck:[\s\S]*http:\/\/localhost\/health/)
  })
}

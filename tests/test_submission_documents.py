import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
from submission_documents import register

class DocumentGateTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  (self.root/'static').mkdir();self.doc=self.root/'static/任务上传文档.md';self.doc.write_text('v1')
  self.app=Flask(__name__,root_path=str(self.root))
  def db():
   c=sqlite3.connect(self.root/'test.db');c.row_factory=sqlite3.Row;return c
  register(self.app,db)
  for route in ('/api/jobs','/api/uploads','/api/uploads/u/content'):
   self.app.add_url_rule(route,route,lambda:('accepted',201),methods=['POST'])
  self.client=self.app.test_client();self.owner=patch('access_control.current_id',return_value='alice');self.owner.start()
 def tearDown(self):self.owner.stop();self.tmp.cleanup()
 def test_version_per_account_and_all_submission_routes(self):
  for route in ('/api/jobs','/api/uploads','/api/uploads/u/content'):
   self.assertEqual(self.client.post(route).status_code,428)
  self.client.head('/api/submission-document/download')
  self.assertTrue(self.client.get('/api/submission-document').json['required'])
  r=self.client.get('/api/submission-document/download');self.assertEqual(r.data,b'v1')
  self.assertFalse(self.client.get('/api/submission-document').json['required'])
  for route in ('/api/jobs','/api/uploads','/api/uploads/u/content'):
   self.assertEqual(self.client.post(route).status_code,201)
  with patch('access_control.current_id',return_value='bob'):
   self.assertTrue(self.client.get('/api/submission-document').json['required'])
  self.doc.write_text('v2')
  self.assertTrue(self.client.get('/api/submission-document').json['required'])
  self.assertEqual(self.client.post('/api/jobs').status_code,428)
  self.assertEqual(self.client.get('/api/submission-document/download').data,b'v2')
  self.assertEqual(self.client.post('/api/jobs').status_code,201)

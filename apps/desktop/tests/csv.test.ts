import { expect, it } from 'vitest';
import { parseCSV } from '../src/state/csv';
it('imports quoted fields, escaped quotes, blank rows and multiline values', () => {
  const rows = parseCSV('name,phone,company\r\n"Reed, Ada",+14155550101,"A ""Good"" Co"\r\n"Multi\nLine",+12125550102,Example\n\n');
  expect(rows).toEqual([{name:'Reed, Ada',phone:'+14155550101',company:'A "Good" Co'},{name:'Multi\nLine',phone:'+12125550102',company:'Example'}]);
});
it('requires phone header and rejects ragged rows', () => {
  expect(() => parseCSV('name\nAda')).toThrow();
  expect(() => parseCSV('name,phone\nAda')).toThrow();
});

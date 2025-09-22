import requests # pyright: ignore[reportMissingModuleSource]
from flask import Flask, render_template_string, request, redirect, jsonify # pyright: ignore[reportMissingModuleImports]
import pandas as pd # pyright: ignore[reportMissingModuleSource]
from datetime import datetime
import traceback
import psutil # pyright: ignore[reportMissingModuleSource]
import os
import signal
import sys
import random
import string
import win32print # pyright: ignore[reportMissingModuleSource]
import qrcode # pyright: ignore[reportMissingModuleSource]
from io import BytesIO
from PIL import Image # pyright: ignore[reportMissingImports]
import threading

app = Flask(__name__)

# === CONFIG ===
excel_file = r"C:\Users\JacobPleasance\OneDrive - Micromix Plant Health Limited\PRODUCTION\A - Jacob Pleasance Files\.MPH Stock WIP (Made by Jacob)\MPH-Stock-Live.xlsx"
qr_codes_file = r"C:\Users\JacobPleasance\OneDrive - Micromix Plant Health Limited\PRODUCTION\A - Jacob Pleasance Files\.MPH Stock WIP (Made by Jacob)\QR-Codes.txt"
printed_qr_codes = set()
qr_generation_lock = threading.Lock()

# === helper startup ===
def load_existing_qr_codes():
    if os.path.exists(qr_codes_file):
        try:
            with open(qr_codes_file, "r", encoding="utf-8") as f:
                for line in f:
                    code = line.strip()
                    if code:
                        printed_qr_codes.add(code)
        except Exception as e:
            print("Could not load existing QR codes:", e)

load_existing_qr_codes()

def terminate_process_on_port(port):
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                connections = proc.connections()
                for conn in connections:
                    if conn.laddr.port == port:
                        os.kill(proc.pid, signal.SIGTERM)
                        print(f"Terminated process {proc.pid} using port {port}")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except Exception as e:
        print(f"Error terminating process on port {port}: {e}")

# === QR helpers (unchanged) ===
def generate_qr_code_id():
    characters = string.ascii_uppercase + string.digits
    with qr_generation_lock:
        while True:
            qr_id = ''.join(random.choice(characters) for _ in range(16))
            if qr_id not in printed_qr_codes:
                printed_qr_codes.add(qr_id)
                try:
                    with open(qr_codes_file, "a", encoding="utf-8") as f:
                        f.write(qr_id + "\n")
                except Exception as e:
                    print(f"Error writing QR code to file: {e}")
                return qr_id

def convert_qr_to_ezpl_bitmap(qr_id):
    qr = qrcode.make(qr_id)
    qr = qr.resize((50, 50), Image.Resampling.LANCZOS)
    qr = qr.convert('1')
    hex_data = []
    for y in range(50):
        byte_data = 0
        bit_position = 7
        for x in range(50):
            pixel = 1 if qr.getpixel((x, y)) == 0 else 0
            byte_data |= (pixel << bit_position)
            bit_position -= 1
            if bit_position < 0:
                hex_data.append(format(byte_data, '02X'))
                byte_data = 0
                bit_position = 7
        if bit_position != 7:
            hex_data.append(format(byte_data, '02X'))
    return ''.join(hex_data)

def print_godex_label(article, item, batch, grn, qr_id):
    ezpl = (
        "^Q50,3\n"
        "^W75\n"
        "^H10\n"
        "^P1\n"
        "^S3\n"
        "^AD\n"
        "^C1\n"
        "^R0\n"
        "~Q+0\n"
        "^O0\n"
        "^L\n"
        f"AA,10,20,2,2,0,0,Article Code: {article}\r\n"
        f"AA,10,70,2,2,0,0,Description: {item}\r\n"
        f"AA,10,120,2,2,0,0,Supplier Batch: {batch}\r\n"
        f"AA,10,170,2,2,0,0,GRN NO: {grn}\r\n"
        f"W360,160,2,2,M,0,11,{len(qr_id)},0\r\n"
        f"{qr_id}\r\n"
        "E\r\n"
    )
    try:
        hPrinter = win32print.OpenPrinter("Godex RT700")
        try:
            win32print.StartDocPrinter(hPrinter, 1, ("GodexLabel", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)
            win32print.WritePrinter(hPrinter, ezpl.encode("utf-8"))
            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)
    except Exception as exc:
        print(f"Printing failed: {exc}")
        print("\n--- RAW EZPL COMMANDS ---\n")
        print(ezpl)

# === API endpoints ===

@app.route('/get-stock-data', methods=['GET'])
def get_stock_data():
    """
    Returns the whole stock as JSON.
    NOTE: we return full data (including 'QR ID' internally) but the front-end will hide QR ID columns.
    """
    try:
        df = pd.read_excel(excel_file, engine='openpyxl')
        df = df.fillna('')
        # ensure QR ID column exists (in case old file doesn't have it)
        if 'QR ID' not in df.columns:
            df['QR ID'] = ''
        return jsonify(df.to_dict('records'))
    except FileNotFoundError:
        print("Excel file not found in get_stock_data.")
        return jsonify([]), 404
    except Exception as e:
        print(f"Error reading Excel file for JSON endpoint: {e}")
        return jsonify([]), 500

@app.route('/search-stock', methods=['GET'])
def search_stock():
    """
    Search by Article Code, PRODUCTS (description), or QR ID.
    If query is empty, return the full stock.
    We reset_index() so that the returned rows include the original dataframe index as 'index' so front-end can identify rows.
    """
    query = request.args.get('q', '').strip().lower()
    try:
        df = pd.read_excel(excel_file, engine='openpyxl')
        df = df.fillna('')
        if 'QR ID' not in df.columns:
            df['QR ID'] = ''
        if query:
            mask = (
                df['Article Code'].astype(str).str.lower().str.contains(query) |
                df['PRODUCTS'].astype(str).str.lower().str.contains(query) |
                df['QR ID'].astype(str).str.lower().str.contains(query)
            )
            results = df[mask]
        else:
            results = df  # return all when empty
        results = results.reset_index()  # keep original index in "index" column
        # convert NaNs
        results = results.fillna('')
        return jsonify(results.to_dict('records'))
    except Exception as e:
        print(f"Error in search_stock: {e}")
        traceback.print_exc()
        return jsonify([]), 500

@app.route('/goods-out', methods=['POST'])
def goods_out():
    """
    Payload:
    {
      "rows": [index1, index2, ...],
      "adjust": {"index1": amount1, "index2": amount2, ...}
    }
    For each selected index:
      - If an adjust amount is provided (numeric):
          subtract amount from Available Quantity. If result <= 0 -> drop the row.
      - If no adjust amount provided: drop the row.
    """
    try:
        payload = request.get_json(force=True)
        selected_ids = payload.get('rows', [])
        adjust_map = payload.get('adjust', {}) or {}
        if not selected_ids:
            return jsonify({"success": False, "error": "No rows provided"}), 400

        # Read df
        df = pd.read_excel(excel_file, engine='openpyxl')
        df = df.fillna('')
        # ensure numeric column exists
        if 'Available Quantity' not in df.columns:
            df['Available Quantity'] = 0

        # operate on a copy index->int mapping
        for sid in selected_ids:
            try:
                orig_index = int(sid)
            except:
                print("Invalid index in goods_out payload:", sid)
                continue
            # find row by original index
            if orig_index not in df.index:
                print("Index not in current df (may have been removed already):", orig_index)
                continue
            row_qty_raw = df.at[orig_index, 'Available Quantity']
            try:
                current_qty = float(row_qty_raw) if row_qty_raw not in (None, '') else 0.0
            except Exception:
                try:
                    current_qty = float(str(row_qty_raw).replace(',', '')) if row_qty_raw else 0.0
                except:
                    current_qty = 0.0

            # read adjust amount for this index if present
            adj_val = adjust_map.get(str(orig_index), '')  # front-end sends keys as strings
            if adj_val is None or str(adj_val).strip() == '':
                # no adjust provided -> remove full row
                df = df.drop(index=orig_index, errors='ignore')
                print(f"Dropped full row {orig_index}")
            else:
                try:
                    adj_num = float(adj_val)
                except Exception:
                    # invalid adjust -> skip
                    print(f"Invalid adjust value for index {orig_index}: {adj_val} - skipping")
                    continue
                new_qty = current_qty - adj_num
                if new_qty <= 0:
                    df = df.drop(index=orig_index, errors='ignore')
                    print(f"Adjusted out entire row {orig_index} (new_qty {new_qty} <= 0)")
                else:
                    df.at[orig_index, 'Available Quantity'] = new_qty
                    # update Date Modified to now
                    df.at[orig_index, 'Date Modified'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    print(f"Reduced index {orig_index} from {current_qty} by {adj_num} -> {new_qty}")

        # after changes, write back. Reset index will happen implicitly by to_excel
        df.to_excel(excel_file, index=False, engine='openpyxl')
        return jsonify({"success": True})
    except Exception as e:
        print("Error in /goods-out:", e)
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

# === Goods In and main UI ===
@app.route('/MPH-Stock/', methods=['GET', 'POST'])
def desktop_index():
    """
    Original 'Goods In' POST flow preserved, with changes:
    - generated QR ID is written into 'QR ID' column in Excel (column L as requested)
    - added print_quantity field to control number of labels printed, allowing 0 to disable printing
    """
    if request.method == 'POST':
        po = request.form.get('po-number')
        grn = request.form.get('grn-number')
        article_code = request.form.get('article-code')
        supplier_batch = request.form.get('batch-number')
        location = request.form.get('location')
        item = request.form.get('item')
        quantity = request.form.get('quantity')
        print_quantity = request.form.get('print-quantity', '1')  # Default to 1 if not provided
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            df = pd.read_excel(excel_file, engine='openpyxl')
            df = df.fillna('')
            if 'QR ID' not in df.columns:
                # Add QR ID as last column if missing
                df['QR ID'] = ''
            mask = (
                (df['P/O'].astype(str) == str(po)) &
                (df['GRN'].astype(str) == str(grn)) &
                (df['Article Code'].astype(str) == str(article_code)) &
                (df['Location'].astype(str) == str(location)) &
                (df['PRODUCTS'].astype(str) == str(item))
            )
            matching_rows = df.loc[mask]
            if not matching_rows.empty:
                try:
                    new_quantity = float(quantity)
                    existing_quantity = (
                        float(matching_rows.iloc[0]['Available Quantity'])
                        if pd.notna(matching_rows.iloc[0]['Available Quantity']) and matching_rows.iloc[0]['Available Quantity'] != ''
                        else 0.0
                    )
                    updated_quantity = existing_quantity + new_quantity
                    df.loc[matching_rows.index[0], 'Available Quantity'] = updated_quantity
                    df.loc[matching_rows.index[0], 'Date Modified'] = current_time
                    df.loc[matching_rows.index[0], 'Supplier Batch'] = supplier_batch
                    # If supplier generated a QR ID earlier for this, leave it; otherwise attach one now if needed
                    if not df.loc[matching_rows.index[0], 'QR ID']:
                        if article_code and item and supplier_batch:
                            qr_id = generate_qr_code_id()
                            df.loc[matching_rows.index[0], 'QR ID'] = qr_id
                            try:
                                print_qty = int(print_quantity) if print_quantity.strip() else 1
                                if print_qty > 0:  # Only print if quantity is positive
                                    for _ in range(print_qty):
                                        print_godex_label(article_code, item, supplier_batch, grn, qr_id)
                            except ValueError:
                                print(f"Invalid print quantity '{print_quantity}', printing 1 label")
                                print_godex_label(article_code, item, supplier_batch, grn, qr_id)
                    print(f"Consolidated stock for matching row: {matching_rows.index[0]}")
                except ValueError:
                    print(f"Warning: Could not convert quantity '{quantity}' to number for consolidation.")
            else:
                new_row = {
                    'Article Code': article_code,
                    'PRODUCTS': item,
                    'P/O': po,
                    'GRN': grn,
                    'Supplier Batch': supplier_batch,
                    'PACK TYPE': '',
                    'Location': location,
                    'Available Quantity': quantity if quantity != '' else 0,
                    'Date Modified': current_time,
                    'Date Counted': current_time,
                    'Allocated Quantity': 0,
                    'QR ID': ''
                }
                # Generate QR ID if all required fields present
                if article_code and item and supplier_batch:
                    qr_id = generate_qr_code_id()
                    new_row['QR ID'] = qr_id
                    try:
                        print_qty = int(print_quantity) if print_quantity.strip() else 1
                        if print_qty > 0:  # Only print if quantity is positive
                            for _ in range(print_qty):
                                print_godex_label(article_code, item, supplier_batch, grn, qr_id)
                    except ValueError:
                        print(f"Invalid print quantity '{print_quantity}', printing 1 label")
                        print_godex_label(article_code, item, supplier_batch, grn, qr_id)
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                print("Added new row:", new_row)
            # Ensure QR ID column exists (safety)
            if 'QR ID' not in df.columns:
                df['QR ID'] = ''
            df.to_excel(excel_file, index=False, engine='openpyxl')
            # redirect back to main route (keeps same behaviour)
            return redirect('/MPH-Stock/')
        except FileNotFoundError:
            print(f"Error in desktop_index POST: Excel file not found at {excel_file}")
            return "Error: Excel file not found.", 500
        except PermissionError:
            print(f"Error in desktop_index POST: Permission denied accessing {excel_file}. Ensure file is not open elsewhere.")
            return "Error: Permission denied accessing Excel file.", 500
        except Exception as e:
            print(f"An error occurred in desktop_index POST: {e}")
            traceback.print_exc()
            return f"An error occurred: {e}", 500

    # GET: render the full UI (client-side will request /get-stock-data and /search-stock)
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>MPH Stock</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
* {box-sizing:border-box;margin:0;padding:0;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}
body {background:linear-gradient(135deg,#f5f7fa 0%,#c3cfe2 100%);min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:20px}
.container {width:100%;max-width:1400px;background:#fff;border-radius:15px;box-shadow:0 10px 30px rgba(0,0,0,.1);overflow:hidden;margin:20px 0}
header {background:#2c3e50;color:#fff;padding:20px;text-align:center}
h1 {font-size:28px;margin-bottom:10px}
.subtitle {font-size:18px;color:#ecf0f1}
.main-menu {display:grid;grid-template-columns:1fr;gap:15px;padding:30px}
.menu-btn {display:flex;align-items:center;justify-content:flex-start;background:#3498db;color:#fff;padding:18px 25px;border-radius:10px;text-decoration:none;font-weight:600;font-size:18px;transition:.3s;width:80%;margin:0 auto;box-shadow:0 4px 6px rgba(0,0,0,.1)}
.menu-btn i {margin-right:15px;font-size:24px}
.menu-btn:hover {transform:translateY(-3px);box-shadow:0 6px 10px rgba(0,0,0,.15)}
.goods-in{background:#3498db}
.move{background:#9b59b6}
.manufacturing{background:#2ecc71}
.goods-out{background:#e74c3c}
.stock-take{background:#f39c12}
.view-stock{background:#16a085}
.goods-in:hover{background:#2980b9}
.move:hover{background:#8e44ad}
.manufacturing:hover{background:#27ae60}
.goods-out:hover{background:#c0392b}
.stock-take:hover{background:#e67e22}
.view-stock:hover{background:#1abc9c}
.content-section {display:none;padding:30px}
.back-btn {background:#7f8c8d;color:#fff;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;margin-bottom:20px;font-weight:600;display:flex;align-items:center}
.back-btn i {margin-right:8px}
.form-group {margin-bottom:20px}
label {display:block;margin-bottom:8px;font-weight:600;color:#2c3e50}
input,select {width:100%;padding:12px 15px;border:1px solid #ddd;border-radius:6px;font-size:16px}
.btn {background:#3498db;color:#fff;border:none;padding:12px 25px;border-radius:6px;cursor:pointer;font-size:16px;font-weight:600;margin-top:10px}
table {width:100%;border-collapse:collapse;margin:20px 0;box-shadow:0 2px 5px rgba(0,0,0,.1)}
th,td {padding:12px 15px;text-align:left;border-bottom:1px solid #ddd}
thead th {background:#2c3e50;color:#fff;position:relative;cursor:pointer}
/* sticky header */
.stock-table-container {overflow:auto;max-height:645px;position:relative;}
.stock-table thead th {
position: sticky;
top: 0;
z-index: 3;
}
/* make checkboxes and adjust input layout nicer */
.goods-out-row {display:flex;gap:10px;align-items:center}
.goods-out-row .row-checkbox {width:30px;flex:0 0 30px}
.goods-out-row .row-cols {display:grid;grid-template-columns:1fr 1fr 120px 120px;gap:8px;align-items:center;width:100%}
.adjust-in {width:100px;padding:6px;border:1px solid #ccc;border-radius:6px}

/* Excel-style filter dropdown styles */
.filter-dropdown {
    position: absolute;
    background: #fff;
    border: 1px solid #aca899;
    box-shadow: 2px 2px 5px rgba(0,0,0,0.2);
    z-index: 1000;
    display: none;
    min-width: 200px;
    font-family: 'Segoe UI', Tahoma, sans-serif;
    font-size: 12px;
    color: #000;
}
.filter-dropdown.show {
    display: block;
}
.filter-header {
    padding: 4px;
    border-bottom: 1px solid #aca899;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.filter-header button {
    background: none;
    border: 1px solid #aca899;
    padding: 2px 6px;
    font-size: 11px;
    cursor: pointer;
    color: #000;
}
.filter-header button:hover {
    background: #e0e0e0;
}
.filter-search-container {
    padding: 4px;
    border-bottom: 1px solid #aca899;
}
.filter-search-container input {
    width: 100%;
    padding: 2px;
    border: 1px solid #aca899;
    font-size: 12px;
    color: #000;
}
.filter-options-container {
    max-height: 200px;
    overflow-y: auto;
}
.filter-options {
    list-style: none;
    padding: 0;
    margin: 0;
    color: #000;
}
.filter-options li {
    padding: 2px 4px 2px 4px;
    cursor: pointer;
    position: relative;
    white-space: nowrap;
    color: #000;
    display: flex;
    align-items: center;
}
.filter-options li:hover {
    background-color: #c1d2ee;
}
.filter-options input[type="checkbox"] {
    margin-right: 4px;
    width: 13px;
    height: 13px;
    cursor: pointer;
}
.filter-options label {
    cursor: pointer;
    flex: 1;
}
.filter-button {
    position: absolute;
    right: 2px;
    top: 50%;
    transform: translateY(-50%);
    width: 16px;
    height: 16px;
    background: #f0f0f0;
    border: 1px solid #aca899;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
}
.filter-button:after {
    content: "▼";
    font-size: 8px;
    color: #000;
}
.filter-button:hover {
    background: #e0e0e0;
}
.filter-indicator {
    position: absolute;
    right: 2px;
    top: 50%;
    transform: translateY(-50%);
    width: 8px;
    height: 8px;
    background: #f39c12;
    border-radius: 50%;
    display: none;
    pointer-events: none;
}
.filter-indicator.active {
    display: block;
}

/* responsive */
@media (max-width:768px){
.menu-btn{width:100%;font-size:16px;padding:15px 20px}
.container{border-radius:10px}
th,td{padding:8px 10px;font-size:14px}
}
</style>
</head>
<body>
<div class="container">
<header>
<h1>MPH Stock</h1>
<p class="subtitle">Stock System by Jacob Pleasance</p>
</header>
<div id="main-content">
<div id="main-menu" class="main-menu" style="display:grid;">
<a href="Goods-In" onclick="event.preventDefault();showSection('goods-in-section');" class="menu-btn goods-in"><i class="fas fa-arrow-down"></i>Goods In (Purchase Orders, Adj In)</a>
<a href="Move-Stock" onclick="event.preventDefault();showSection('move-section');" class="menu-btn move"><i class="fas fa-exchange-alt"></i>Move Stock</a>
<a href="Manufacturing" onclick="event.preventDefault();showSection('manufacturing-section');" class="menu-btn manufacturing"><i class="fas fa-industry"></i>Manufacturing</a>
<a href="Goods-Out" onclick="event.preventDefault();showSection('goods-out-section');" class="menu-btn goods-out"><i class="fas fa-arrow-up"></i>Goods Out (Sales Orders, Adj Out)</a>
<a href="Stock-Take" onclick="event.preventDefault();showSection('stock-take-section');" class="menu-btn stock-take"><i class="fas fa-clipboard-check"></i>Stock Take</a>
<a href="Stock" onclick="event.preventDefault();showSection('view-stock-section');" class="menu-btn view-stock"><i class="fas fa-boxes"></i>View Current Stock</a>
</div>

<!-- GOODS IN -->
<div id="goods-in-section" class="content-section">
<a href="#" onclick="event.preventDefault();showSection('main-menu');" class="back-btn"><i class="fas fa-arrow-left"></i> Back to Main Menu</a>
<h2>Goods In</h2>
<form method="POST">
<div class="goods-in-form-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
<div class="form-group"><label for="po-number">PO:</label><input type="text" id="po-number" name="po-number" placeholder="Order Number"></div>
<div class="form-group"><label for="grn-number">GRN Reference:</label><input type="text" id="grn-number" name="grn-number" placeholder="Goods Received Note Number"></div>
<div class="form-group"><label for="article-code">Article Code:</label><input type="text" id="article-code" name="article-code" placeholder="Article Code"></div>
<div class="form-group"><label for="batch-number">Batch Number:</label><input type="text" id="batch-number" name="batch-number" placeholder="Batch Number"></div>
<div class="form-group"><label for="location">Location:</label><input type="text" id="location" name="location" placeholder="Location"></div>
<div class="form-group"><label for="item">Item:</label><input type="text" id="item" name="item" placeholder="Item Name or Article Code"></div>
<div class="form-group"><label for="quantity">Quantity:</label><input type="number" id="quantity" name="quantity" placeholder="Quantity"></div>
<div class="form-group"><label for="print-quantity">Print Quantity:</label><input type="number" id="print-quantity" name="print-quantity" placeholder="Number of labels to print (0 to disable)" value="1" min="0"></div>
<div style="align-self:end"><button type="submit" class="btn">Submit</button></div>
</div>
</form>
</div>

<!-- MOVE -->
<div id="move-section" class="content-section" style="display:none;">
<a href="#" onclick="event.preventDefault();showSection('main-menu');" class="back-btn"><i class="fas fa-arrow-left"></i> Back to Main Menu</a>
<h2>Move Stock</h2><p>This is the Move Stock page.</p>
</div>

<!-- MANUFACTURING -->
<div id="manufacturing-section" class="content-section" style="display:none;">
<a href="#" onclick="event.preventDefault();showSection('main-menu');" class="back-btn"><i class="fas fa-industry"></i>Manufacturing</a>
<h2>Manufacturing</h2><p>This is the Manufacturing page.</p>
</div>

<!-- GOODS OUT -->
<div id="goods-out-section" class="content-section" style="display:none;">
<a href="#" onclick="event.preventDefault();showSection('main-menu');" class="back-btn"><i class="fas fa-arrow-up"></i>Goods Out (Sales Orders, Adj Out)</a>
<h2>Goods Out</h2>

<div class="form-group" style="display:flex;gap:8px;align-items:center;">
<div style="flex:1">
<label for="goods-out-search">Search by Article Code, Item Description or QR ID (leave empty to show all):</label>
<input type="text" id="goods-out-search" placeholder="Enter article code / description / QR ID" />
</div>
<div style="width:140px;display:flex;flex-direction:column;align-items:flex-end;justify-content:flex-end;">
<button class="btn" style="height:44px" onclick="searchGoodsOut()">Search</button>
</div>
</div>

<form id="goods-out-form" onsubmit="submitGoodsOut(event)">
<div id="goods-out-results" class="stock-table-container">
<p>Search to display stock...</p>
</div>
<div style="margin-top:10px">
<button type="submit" class="btn">Confirm Goods Out / Adjust</button>
</div>
</form>
</div>

<!-- STOCK TAKE -->
<div id="stock-take-section" class="content-section" style="display:none;">
<a href="#" onclick="event.preventDefault();showSection('main-menu');" class="back-btn"><i class="fas fa-clipboard-check"></i>Stock Take</a>
<h2>Stock Take</h2><p>This is the Stock Take page.</p>
</div>

<!-- VIEW STOCK -->
<div id="view-stock-section" class="content-section" style="display:none;">
<a href="#" onclick="event.preventDefault();showSection('main-menu');" class="back-btn"><i class="fas fa-arrow-left"></i> Back to Main Menu</a>
<h2>View Current Stock</h2>
<div id="stock-table-container" class="stock-table-container">
<table id="stock-table" class="stock-table" style="width:100%">
<thead>
<tr>
<th data-column="Article Code">Article Code
    <div class="filter-button" onclick="toggleFilterDropdown('Article Code', event)"></div>
    <div class="filter-indicator" id="indicator-Article Code"></div>
</th>
<th data-column="PRODUCTS">Item
    <div class="filter-button" onclick="toggleFilterDropdown('PRODUCTS', event)"></div>
    <div class="filter-indicator" id="indicator-PRODUCTS"></div>
</th>
<th data-column="P/O">P/O
    <div class="filter-button" onclick="toggleFilterDropdown('P/O', event)"></div>
    <div class="filter-indicator" id="indicator-P/O"></div>
</th>
<th data-column="GRN">GRN
    <div class="filter-button" onclick="toggleFilterDropdown('GRN', event)"></div>
    <div class="filter-indicator" id="indicator-GRN"></div>
</th>
<th data-column="Supplier Batch">Supplier Batch
    <div class="filter-button" onclick="toggleFilterDropdown('Supplier Batch', event)"></div>
    <div class="filter-indicator" id="indicator-Supplier Batch"></div>
</th>
<th data-column="PACK TYPE">Pack Type
    <div class="filter-button" onclick="toggleFilterDropdown('PACK TYPE', event)"></div>
    <div class="filter-indicator" id="indicator-PACK TYPE"></div>
</th>
<th data-column="Location">Location
    <div class="filter-button" onclick="toggleFilterDropdown('Location', event)"></div>
    <div class="filter-indicator" id="indicator-Location"></div>
</th>
<th data-column="Available Quantity">Available Quantity</th>
<th data-column="Date Modified">Date Modified</th>
<th data-column="Date Counted">Date Counted</th>
<th data-column="Allocated Quantity">Allocated Quantity
    <div class="filter-button" onclick="toggleFilterDropdown('Allocated Quantity', event)"></div>
    <div class="filter-indicator" id="indicator-Allocated Quantity"></div>
</th>
</tr>
</thead>
<tbody></tbody>
</table>
</div>
</div>

<!-- Filter dropdowns positioned outside the table -->
<div class="filter-dropdown" id="dropdown-Article Code">
    <div class="filter-header">
        <button onclick="clearColumnFilters('Article Code')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('Article Code')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-Article Code"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-PRODUCTS">
    <div class="filter-header">
        <button onclick="clearColumnFilters('PRODUCTS')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('PRODUCTS')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-PRODUCTS"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-P/O">
    <div class="filter-header">
        <button onclick="clearColumnFilters('P/O')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('P/O')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-P/O"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-GRN">
    <div class="filter-header">
        <button onclick="clearColumnFilters('GRN')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('GRN')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-GRN"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-Supplier Batch">
    <div class="filter-header">
        <button onclick="clearColumnFilters('Supplier Batch')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('Supplier Batch')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-Supplier Batch"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-PACK TYPE">
    <div class="filter-header">
        <button onclick="clearColumnFilters('PACK TYPE')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('PACK TYPE')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-PACK TYPE"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-Location">
    <div class="filter-header">
        <button onclick="clearColumnFilters('Location')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('Location')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-Location"></ul>
    </div>
</div>
<div class="filter-dropdown" id="dropdown-Allocated Quantity">
    <div class="filter-header">
        <button onclick="clearColumnFilters('Allocated Quantity')">Clear Filter</button>
    </div>
    <div class="filter-search-container">
        <input type="text" placeholder="Search..." oninput="filterOptions('Allocated Quantity')">
    </div>
    <div class="filter-options-container">
        <ul class="filter-options" id="options-Allocated Quantity"></ul>
    </div>
</div>

</div>
</div>

<script>
/*
Client-side logic:
- showSection: simple navigation
- loadStock: loads all stock for View Stock (hides 'QR ID' column)
- searchGoodsOut: searches (by code, description, or QR) and displays rows.
- persist selections and per-row adjust amounts across searches using JS maps.
- submitGoodsOut: sends adjustments + selected rows to /goods-out.
- Dates shown formatted dd/mm/yyyy
*/

let stockInterval = null;
let allStockData = []; // Store all stock data for filtering
let filteredStockData = []; // Store currently filtered data
let activeFilters = {}; // Store active filters {columnName: [selectedValues]}
let currentDropdownColumn = null; // Track which column's dropdown is open

// Keep track of selected rows (original dataframe indexes) as strings
const selectedRows = new Set();
// Keep track of adjust values by index (strings)
const adjustMap = {}; // e.g. {"123": "5"}

function escapeHtml(unsafe) {
    return unsafe.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function showSection(sectionId){
document.querySelectorAll('.main-menu,.content-section').forEach(el=>el.style.display='none');
const sec = document.getElementById(sectionId);
if(!sec) return;
sec.style.display = 'block';
// if viewing stock, load stock and refresh periodically
if(sectionId === 'view-stock-section'){
loadStock();
if(stockInterval) clearInterval(stockInterval);
stockInterval = setInterval(loadStock, 10000);
} else {
if(stockInterval) { clearInterval(stockInterval); stockInterval = null; }
}
}

window.onload = () => {
showSection('main-menu');
// Add click outside listener to close dropdowns
document.addEventListener('click', function(event) {
    const dropdowns = document.querySelectorAll('.filter-dropdown');
    dropdowns.forEach(dropdown => {
        if (!dropdown.contains(event.target) && !event.target.closest('.filter-button')) {
            dropdown.classList.remove('show');
        }
    });
});
};

// helper: friendly dd/mm/yyyy display
function formatDateDisplay(isoDateStr){
if(!isoDateStr) return '';
// try to parse common formats
try{
const d = new Date(isoDateStr);
if(isNaN(d)) {
// if not parseable, attempt manual split (yyyy-mm-dd)
if(typeof isoDateStr === 'string' && isoDateStr.includes('-')){
const parts = isoDateStr.split(' ')[0].split('-');
if(parts.length >= 3){
return `${parts[2]}/${parts[1]}/${parts[0]}`;
}
}
return isoDateStr;
}
const dd = String(d.getDate()).padStart(2,'0');
const mm = String(d.getMonth()+1).padStart(2,'0');
const yyyy = d.getFullYear();
return `${dd}/${mm}/${yyyy}`;
} catch (e){
return isoDateStr;
}
}

// load full stock for the View Stock page
async function loadStock(){
try{
const resp = await fetch('/get-stock-data');
const data = await resp.json();
allStockData = data;
filteredStockData = data;
renderStockTable(data);
generateFilterOptions();
}catch(err){
console.error('Error loading stock:', err);
}
}

// Generate filter options for each column
function generateFilterOptions() {
    const columns = ['Article Code', 'PRODUCTS', 'P/O', 'GRN', 'Supplier Batch', 'PACK TYPE', 'Location', 'Allocated Quantity'];
    
    columns.forEach(column => {
        const values = [...new Set(allStockData.map(row => String(row[column] ?? '')))];
        values.sort();
        
        const optionsList = document.getElementById(`options-${column}`);
        if (!optionsList) return; // Skip if element doesn't exist
        
        optionsList.innerHTML = '';
        
        // Add individual options
        values.forEach(value => {
            if (value === '') return; // Skip empty values
            const li = document.createElement('li');
            const chkId = `chk-${column.replace(/[^a-zA-Z0-9]/g, '-')}-${value.replace(/[^a-zA-Z0-9]/g, '-')}`;
            li.innerHTML = `<input type="checkbox" id="${chkId}" data-value="${escapeHtml(value)}">
                            <label for="${chkId}">${escapeHtml(value)}</label>`;
            li.dataset.value = value;
            
            const checkbox = li.querySelector('input');
            
            // Check if this value is selected in active filters
            if (activeFilters[column] && activeFilters[column].includes(value)) {
                checkbox.checked = true;
            }
            
            checkbox.addEventListener('change', () => toggleFilterSelection(column, value, checkbox));
            optionsList.appendChild(li);
        });
        
        // Update filter indicator
        updateFilterIndicator(column);
    });
}

// Toggle filter dropdown
function toggleFilterDropdown(column, event) {
    event.stopPropagation();
    currentDropdownColumn = column;
    
    // Position the dropdown correctly
    positionDropdown(column);
    
    const dropdown = document.getElementById(`dropdown-${column}`);
    
    // Close all other dropdowns
    document.querySelectorAll('.filter-dropdown').forEach(d => {
        if (d !== dropdown) d.classList.remove('show');
    });
    
    // Toggle current dropdown
    dropdown.classList.toggle('show');
    
    // Focus search input
    const searchInput = dropdown.querySelector('input');
    if (dropdown.classList.contains('show')) {
        searchInput.focus();
    }
}

// Position dropdown relative to its header button
function positionDropdown(column) {
    const headerButton = document.querySelector(`th[data-column="${column}"] .filter-button`);
    const dropdown = document.getElementById(`dropdown-${column}`);
    
    if (headerButton && dropdown) {
        const rect = headerButton.getBoundingClientRect();
        
        // Position dropdown relative to the document
        dropdown.style.left = (rect.left + window.scrollX) + 'px';
        dropdown.style.top = (rect.top + rect.height + window.scrollY) + 'px';
        dropdown.style.minWidth = rect.width + 'px';
    }
}

// Toggle filter selection
function toggleFilterSelection(column, value, checkbox) {
    if (!activeFilters[column]) {
        activeFilters[column] = [];
    }
    
    if (checkbox.checked) {
        // Add filter
        if (!activeFilters[column].includes(value)) {
            activeFilters[column].push(value);
        }
    } else {
        // Remove filter
        const index = activeFilters[column].indexOf(value);
        if (index > -1) {
            activeFilters[column].splice(index, 1);
        }
    }
    
    // Update filter indicator
    updateFilterIndicator(column);
    
    // Apply filters immediately
    applyFilters();
}

// Clear filters for a specific column
function clearColumnFilters(column) {
    activeFilters[column] = [];
    generateFilterOptions(); // Regenerate to uncheck all checkboxes
    applyFilters();
    
    // Hide the dropdown after clearing
    const dropdown = document.getElementById(`dropdown-${column}`);
    if (dropdown) {
        dropdown.classList.remove('show');
    }
}

// Update filter indicator count
function updateFilterIndicator(column) {
    const count = activeFilters[column] ? activeFilters[column].length : 0;
    const indicator = document.getElementById(`indicator-${column}`);
    if (indicator) {
        if (count > 0) {
            indicator.classList.add('active');
        } else {
            indicator.classList.remove('active');
        }
    }
}

// Apply all active filters with OR logic
function applyFilters() {
    // If no filters are active, show all data
    const hasActiveFilters = Object.values(activeFilters).some(filters => filters && filters.length > 0);
    
    if (!hasActiveFilters) {
        filteredStockData = [...allStockData];
        renderStockTable(filteredStockData);
        return;
    }
    
    // Apply AND logic between columns: show rows that match ALL active column filters
    let filtered = allStockData.filter(row => {
        // For each column with active filters, check if the row matches at least one value
        for (const column in activeFilters) {
            if (activeFilters[column] && activeFilters[column].length > 0) {
                const rowValue = String(row[column] ?? '');
                // If row doesn't match any of the selected values in this column, exclude it
                if (!activeFilters[column].includes(rowValue)) {
                    return false;
                }
            }
        }
        return true; // Row matches all active filters
    });
    
    filteredStockData = filtered;
    renderStockTable(filtered);
}

// Filter options based on search input
function filterOptions(column) {
    const input = event.target;
    const filter = input.value.toUpperCase();
    const optionsContainer = document.getElementById(`dropdown-${column}`).querySelector('.filter-options-container');
    const options = optionsContainer.getElementsByTagName('li');
    
    for (let i = 0; i < options.length; i++) {
        const txtValue = options[i].textContent || options[i].innerText;
        options[i].style.display = txtValue.toUpperCase().indexOf(filter) > -1 ? "" : "none";
    }
}

// Render stock table with given data
function renderStockTable(data) {
    const tbody = document.querySelector('#stock-table tbody');
    tbody.innerHTML = '';
    
    data.forEach(row => {
        const tr = document.createElement('tr');
        // hide QR ID intentionally (not shown)
        tr.innerHTML = `
        <td>${row['Article Code'] ?? ''}</td>
        <td>${row['PRODUCTS'] ?? ''}</td>
        <td>${row['P/O'] ?? ''}</td>
        <td>${row['GRN'] ?? ''}</td>
        <td>${row['Supplier Batch'] ?? ''}</td>
        <td>${row['PACK TYPE'] ?? ''}</td>
        <td>${row['Location'] ?? ''}</td>
        <td>${row['Available Quantity'] ?? ''}</td>
        <td>${formatDateDisplay(row['Date Modified'] ?? '')}</td>
        <td>${formatDateDisplay(row['Date Counted'] ?? '')}</td>
        <td>${row['Allocated Quantity'] ?? ''}</td>
        `;
        tbody.appendChild(tr);
    });
}

// GOODS OUT: keep selections across searches
function renderGoodsOutTable(rows) {
// rows is an array of objects which include 'index' (original df index)
const container = document.getElementById('goods-out-results');
if(!rows || rows.length === 0){
container.innerHTML = '<p>No matching stock found.</p>';
return;
}
// Build table with checkboxes and adjust input
let html = '<table class="stock-table"><thead><tr><th style="width:40px"></th><th>Article Code</th><th>Item</th><th>Batch</th><th>Location</th><th>Qty</th><th>Adjust Out</th></tr></thead><tbody>';
rows.forEach(row => {
const idx = row['index'];
const checked = selectedRows.has(String(idx)) ? 'checked' : '';
const adjVal = (adjustMap[String(idx)] !== undefined) ? adjustMap[String(idx)] : '';
const qty = (row['Available Quantity'] === null || row['Available Quantity'] === undefined) ? '' : row['Available Quantity'];
html += `<tr data-idx="${idx}">
<td><input type="checkbox" class="go-checkbox" data-idx="${idx}" ${checked}></td>
<td>${row['Article Code'] ?? ''}</td>
<td>${row['PRODUCTS'] ?? ''}</td>
<td>${row['Supplier Batch'] ?? ''}</td>
<td>${row['Location'] ?? ''}</td>
<td>${qty}</td>
<td><input type="number" min="0" step="any" class="adjust-in" data-idx="${idx}" value="${adjVal}"></td>
</tr>`;
});
html += '</tbody></table>';
container.innerHTML = html;

// attach event handlers for checkboxes and adjust inputs
document.querySelectorAll('.go-checkbox').forEach(cb => {
cb.addEventListener('change', (e) => {
const idx = e.target.getAttribute('data-idx');
if(e.target.checked) selectedRows.add(String(idx));
else {
selectedRows.delete(String(idx));
// also clear adjust value for removed row
if(adjustMap.hasOwnProperty(String(idx))) delete adjustMap[String(idx)];
}
});
});
document.querySelectorAll('.adjust-in').forEach(inp => {
inp.addEventListener('input', (e) => {
const idx = e.target.getAttribute('data-idx');
const val = e.target.value;
if(val === '' || val === null) {
if(adjustMap.hasOwnProperty(String(idx))) delete adjustMap[String(idx)];
} else {
adjustMap[String(idx)] = val;
}
});
});
}

// search (Article Code, PRODUCTS, or QR ID). If box empty -> fetch all
async function searchGoodsOut(){
const q = document.getElementById('goods-out-search').value.trim();
const container = document.getElementById('goods-out-results');
container.innerHTML = '<p>Searching...</p>';
try{
const resp = await fetch('/search-stock?q=' + encodeURIComponent(q));
const data = await resp.json();
// show results (but hide QR ID column; data will still have it)
renderGoodsOutTable(data);
}catch(err){
console.error('Error searching stock:', err);
container.innerHTML = '<p>Error searching stock.</p>';
}
}

// submit goods out / adjustments
async function submitGoodsOut(e){
e.preventDefault();
if(selectedRows.size === 0){
alert('No lines selected');
return;
}
// prepare payload
const rows = Array.from(selectedRows);
const adjust = {};
for(const k of Object.keys(adjustMap)){
// only include adjusts for rows that are selected
if(selectedRows.has(String(k))) adjust[String(k)] = adjustMap[k];
}
try{
const resp = await fetch('/goods-out', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({rows: rows, adjust: adjust})
});
const result = await resp.json();
if(result.success){
alert('Goods Out processed');
// clear selections & adjustments
selectedRows.clear();
for(const k in adjustMap) delete adjustMap[k];
// go back to main menu like you requested
showSection('main-menu');
} else {
alert('Error: ' + (result.error || 'Unknown error'));
}
}catch(err){
alert('Request failed: ' + err);
}
}

// Handle window resize to reposition dropdowns
window.addEventListener('resize', function() {
    if (currentDropdownColumn) {
        positionDropdown(currentDropdownColumn);
    }
});

// Handle scroll to reposition dropdowns
document.getElementById('stock-table-container').addEventListener('scroll', function() {
    if (currentDropdownColumn) {
        positionDropdown(currentDropdownColumn);
    }
});

</script>
</body>
</html>
    """)

# === Run server ===
if __name__ == '__main__':
    PORT = 1567
    terminate_process_on_port(PORT)
    try:
        app.run(host='0.0.0.0', port=PORT, debug=True, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("Flask server stopped.")
        sys.exit(0)

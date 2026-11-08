"""Polyglot landscape (PRD FR-040): the four Release 4 adapters linked across repositories.

pricing-service   Go (net/http + chi-style mux): GET /prices/{id} returns Price {id, amount, currency}; Go test with httptest
orders-service    TypeScript (NestJS): POST /orders (CreateOrderDto with class-validator rules) calls pricing through process.env.PRICING_URL;
                  jest + supertest test
billing-service   Kotlin (Spring): calls pricing through @Value("${pricing.url}") with RestTemplate; calls ledger
ledger-service    C# (ASP.NET Core controller): POST /api/entries; xUnit test

Branches on pricing-service: field_removed (Price loses currency: breaking for orders and billing), impl_only (the handler body changes).
"""
from __future__ import annotations

import json
from pathlib import Path

from fixtures.build_fixture import _commit, _run, _write

PRICING = '''package main

import (
	"encoding/json"
	"net/http"
	"os"
)

type Price struct {
	ID       string  `json:"id" validate:"required"`
	Amount   float64 `json:"amount" validate:"required,min=0"`
	Currency string  `json:"currency" validate:"required,oneof=EUR USD"`
}

func getPrice(w http.ResponseWriter, r *http.Request) {
	p := Price{ID: r.PathValue("id"), Amount: 10, Currency: "EUR"}
	json.NewEncoder(w).Encode(p)
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /prices/{id}", getPrice)
	http.ListenAndServe(os.Getenv("ADDR"), mux)
}
'''

PRICING_TEST = '''package main

import (
	"net/http/httptest"
	"testing"
)

func TestGetPrice(t *testing.T) {
	req := httptest.NewRequest("GET", "/prices/p1", nil)
	w := httptest.NewRecorder()
	getPrice(w, req)
}
'''

ORDERS = '''import { Body, Controller, Post, UseGuards } from '@nestjs/common';
import axios from 'axios';

@Controller('orders')
@UseGuards(JwtGuard)
export class OrdersController {
  @Post()
  async create(@Body() dto: CreateOrderDto): Promise<Order> {
    const price = await this.priceOf(dto.sku);
    return { id: 'o1', sku: dto.sku, total: price.amount, currency: price.currency };
  }

  async priceOf(sku: string): Promise<Price> {
    const r = await axios.get(`${process.env.PRICING_URL}/prices/${sku}`);
    return r.data;
  }
}

export class CreateOrderDto {
  @IsString() @MaxLength(18) sku: string;
  @Min(1) quantity: number;
}

export interface Order { id: string; sku: string; total: number; currency: string }
export interface Price { id: string; amount: number; currency: string }
'''

ORDERS_TEST = '''const request = require('supertest');

describe('orders', () => {
  it('creates an order', async () => {
    await request(app).post('/orders').send({ sku: 'A1', quantity: 1 }).expect(201);
  });
});
'''

BILLING = '''package com.acme.billing

import org.springframework.beans.factory.annotation.Value
import org.springframework.web.client.RestTemplate

class BillingService(private val rest: RestTemplate) {
    @Value("\\${pricing.url}") lateinit var pricingUrl: String
    @Value("\\${ledger.url}") lateinit var ledgerUrl: String

    fun bill(sku: String): Double {
        val p = rest.getForObject("$pricingUrl/prices/$sku", PriceDto::class.java)!!
        rest.postForObject("$ledgerUrl/api/entries", p, String::class.java)
        return p.amount
    }
}

data class PriceDto(val id: String, val amount: Double, val currency: String)
'''

LEDGER = '''using Microsoft.AspNetCore.Mvc;
using System.ComponentModel.DataAnnotations;

namespace Ledger.Controllers;

[ApiController]
[Route("api/[controller]")]
public class EntriesController : ControllerBase
{
    [HttpPost]
    public ActionResult<Entry> Post([FromBody] Entry entry) { return entry; }
}

public class Entry
{
    [Required] [StringLength(18)] public string Id { get; set; }
    [Range(0, 1000000)] public decimal Amount { get; set; }
}
'''

LEDGER_TEST = '''using Xunit;

public class EntriesTests
{
    [Fact]
    public async void PostsAnEntry()
    {
        var r = await client.PostAsync("/api/entries", null);
    }
}
'''


def build(dest: str) -> dict:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    out = {"paths": {}, "shas": {}}

    def repo(name, files):
        p = root / name
        p.mkdir(parents=True, exist_ok=True)
        _run(p, "init", "-q", "-b", "main")
        _write(p, files)
        out["paths"][name] = str(p)
        return p, _commit(p, "initial")

    p, base = repo("pricing-service", {"main.go": PRICING, "main_test.go": PRICING_TEST, "go.mod": "module pricing\n\ngo 1.22\n",
                                       "tia-services.yaml": "service: pricing-service\ncriticality: critical\n"})
    out["shas"]["base"] = base
    repo("orders-service", {"src/orders.controller.ts": ORDERS, "test/orders.test.js": ORDERS_TEST, "package.json": "{}\n",
                            "tia-services.yaml": "service: orders-service\nconfig_keys:\n  PRICING_URL: pricing-service\n"})
    repo("billing-service", {"src/main/kotlin/com/acme/billing/BillingService.kt": BILLING,
                             "tia-services.yaml": "service: billing-service\nconfig_keys:\n  pricing.url: pricing-service\n  ledger.url: ledger-service\n"})
    repo("ledger-service", {"Controllers/EntriesController.cs": LEDGER, "Tests/EntriesTests.cs": LEDGER_TEST,
                            "tia-services.yaml": "service: ledger-service\n"})

    def branch(name, files):
        _run(p, "checkout", "-q", "-b", name, base)
        _write(p, files)
        out["shas"][name] = _commit(p, name)

    branch("field_removed", {"main.go": PRICING.replace('\tCurrency string  `json:"currency" validate:"required,oneof=EUR USD"`\n', "")
                                              .replace(', Currency: "EUR"', "")})
    branch("impl_only", {"main.go": PRICING.replace("Amount: 10,", "Amount: 12,")})
    _run(p, "checkout", "-q", "main")
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(build(sys.argv[1]), indent=2))

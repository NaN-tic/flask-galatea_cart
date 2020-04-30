#!/usr/bin/env python
from flask import url_for

def add_cart(self):
    '''Add cart'''
    # reset password
    response = self.client.post(url_for('cart.add', lang=self.language),
        data=self.products, follow_redirects=True)
    assert 'been added in your cart' in str(response.data)

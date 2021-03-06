# -*- coding: utf-8 -*-

import datetime
import logging
from dateutil.relativedelta import relativedelta

from openerp import models, fields, api
from openerp.addons import account

from .tools.PySepaDD import PySepaDD

logger = logging.getLogger(__name__)


class alkivi_sepa(models.Model):
    _name = 'alkivi.sepa'
    _description = 'SEPA Report'

    name = fields.Text(string='Description')
    date = fields.Datetime(string='Mandat creation date', required=True, default=datetime.datetime.today())
    collection_date = fields.Datetime(string='Mandat collection date', required=True, default=lambda self: self._get_collection_date())
    line_ids = fields.One2many('alkivi.sepa.line', 'sepa_id', string="Lines")

    @api.model
    def _get_collection_date(self):
        """
        Set the default collection date at:
        - the 20 of current month
        - ensuring at least 5 days from now
        """
        today = datetime.datetime.today()
        min_interval = datetime.timedelta(days=5)

        wanted_date = today.replace(day = 20)
        if wanted_date - today < min_interval:
            wanted_date = today + min_interval
        return wanted_date

    @api.multi
    def generate_xml(self):
        """Will take all the data and generate a file to download."""
        self.ensure_one()

        config = {
                "name": self.env['ir.values'].get_default('alkivi.sepa.config', 'bank_name'),
                "IBAN": self.env['ir.values'].get_default('alkivi.sepa.config','iban'),
                "BIC": self.env['ir.values'].get_default('alkivi.sepa.config','bic'),
                "batch": True,
                "creditor_id": self.env['ir.values'].get_default('alkivi.sepa.config','creditor_id'),
                "currency": self.env['ir.values'].get_default('alkivi.sepa.config','currency'),
        }
        sepa = PySepaDD(config)

        for line in self.line_ids:
            invoice = line.invoice_id
            partner = line.partner_id

            collection_date = datetime.datetime.strptime(self.collection_date, '%Y-%m-%d %H:%M:%S').date()
            mandate_date = datetime.datetime.strptime(partner.mandat_creation_date, '%Y-%m-%d %H:%M:%S').date()
            #invoice.amount_total has to be formated like that for the sepa or you will have round problems
            amount = '%.2f' % (invoice.amount_total*100)
            amount = float(amount)
            amount = int(amount)
            payment = {
                    "name": partner.sepa_name,
                    "IBAN": partner.iban,
                    "BIC": partner.bic,
                    "amount": amount,
                    "type": "RCUR",
                    "collection_date": collection_date,
                    "mandate_id": partner.rum,
                    "mandate_date": mandate_date,
                    "description": 'Facture Alkivi {0}'.format(invoice.number),
            }
            sepa.add_payment(payment)

        return sepa.export()

    @api.multi
    def get_xml(self):
        return {
                'type' : 'ir.actions.act_url',
                'url': '/web/binary/download_sepa?id=%s&format=xml'%(self.id),
                'target': 'self',
        }                  

    @api.multi
    def get_period(self, date):
        """
            Return date period 
        """
        # Fetch correct period_id according to transaction date
        search_args = [('date_start', '<=', date), ('date_stop', '>=', date), ('special', '=', False)]
        period_ids = self.env['account.period'].search(search_args)
        logger.debug('Period possible : {0}'.format(period_ids))
        if not period_ids:
            raise osv.except_osv(_("Warning"), _("Unable to find a period for today  %s" % date))
        elif len(period_ids) > 1:
            raise osv.except_osv(_("Warning"), _("Found multiple period for today %s" % date))
        return period_ids[0].id

    @api.multi
    def pay_invoices(self):
        """
        Mark as paid all invoices that has been paid trough this sepa
        """
        logger.debug('Pay all invoices of mandat id:{0}'.format(self.id))
        amount_to_reconciliate = 0
        for line in self.line_ids:
            invoice = line.invoice_id
            amount_to_reconciliate += round(invoice.amount_total,2)
            if invoice.state == 'open':
                voucher_id = self.pay_invoice(invoice)
                self.validate_voucher_moves(voucher_id)
            else:
                logger.info('We can\'t mark as paid invoice id:{0}. It is not open state'.format(invoice.id))
        
        logger.debug('We will create a Account Move for : {0}€'.format(amount_to_reconciliate))
        journal_id = int(self.env['ir.values'].get_default('alkivi.sepa.config','journal_id'))
        account_id = int(self.env['ir.values'].get_default('alkivi.sepa.config','bank_account_id'))
        move = self.env['account.move'].create({
            'journal_id': journal_id,
            'date': self.date,
            'state': 'posted',
        })
        period_id = self.get_period(self.date)
        logger.debug('We created an Account Move id : {}'.format(move))
        credit_move_line = {
                    'journal_id': journal_id,
                    'name': '/',
                    'account_id': account_id,
                    'move_id': move.id,
                    'period_id': period_id,
                    'quantity': 1,
                    'credit': amount_to_reconciliate,
                    'debit': 0.0,
                    'date': self.date,
                    'amount_currency': 0.0,
        }
        logger.debug('Credit_line to be created : {0}'.format(credit_move_line))
        credit_move_line = self.env['account.move.line'].create(credit_move_line)
        debit_move_line ={
                    'journal_id': journal_id,
                    'name': '/',
                    'account_id': account_id,
                    'move_id': move.id,
                    'period_id': period_id,
                    'quantity': 1,
                    'debit': amount_to_reconciliate,
                    'credit': 0.0,
                    'date': self.date,
                    'amount_currency': 0.0,
        }
        credit_move_line = self.env['account.move.line'].create(debit_move_line)
        move.post()

    @api.multi
    def validate_voucher_moves(self, voucher_id):
        logger.debug('We will mark voucher id:{} moves as validated'.format(voucher_id))        
        moves = self.env['account.move'].search([['id', '=', voucher_id.move_id.id]])
        for move in moves:
            move.state = 'posted'

    @api.multi
    def pay_invoice(self, invoice):
        logger.debug('We will mark invoice id:{} as paid'.format(invoice))        
        
        # Fetch correct period_id according to transaction date
        date = self.date
        period_id = self.get_period(self.date)
        partner_id = self.env['res.partner']._find_accounting_partner(invoice.partner_id).id,
        journal_id = self.env['ir.values'].get_default('alkivi.sepa.config','journal_id')
        account_id = self.env['ir.values'].get_default('alkivi.sepa.config','bank_account_id')
        voucher_data = {
            'partner_id': partner_id[0],
            'amount': invoice.amount_total,
            'journal_id': journal_id, #CIC Lille id, to put in config
            'date': date,
            'period_id': period_id,
            'account_id': account_id, #Cic Account, to put in config
            'type': invoice.type in ('out_invoice','out_refund') and 'receipt' or 'payment',
            'reference' : invoice.name,
        }

        logger.debug('voucher_data')
        logger.debug(voucher_data)

        voucher_id = self.env['account.voucher'].create(voucher_data)

        # Need to create basic account.voucher.line according to the type of invoice need to check stuff ...
        double_check = 0
        for move_line in invoice.move_id.line_id:
            logger.debug('Analysing move_line %d' % move_line.id)
            if move_line.product_id:
                logger.debug('Skipping move_line %d because got product_id and we dont want that' % move_line.id)
                continue

            # According to invoice type 
            if invoice.type in ('out_invoice','in_refund'):
                if move_line.debit > 0.0:
                    line_data = {
                        'name': invoice.number,
                        'voucher_id' : voucher_id.id,
                        'move_line_id' : move_line.id,
                        'account_id' : invoice.account_id.id,
                        'partner_id' : partner_id,
                        'amount_unreconciled': abs(move_line.debit),
                        'amount_original': abs(move_line.debit),
                        'amount': abs(move_line.debit),
                        'type': 'cr',
                    }
                    logger.debug('line_data')
                    logger.debug(line_data)

                    line_id = self.env['account.voucher.line'].create(line_data)
                    double_check += 1
        
        # Cautious check to see if we did ok
        if double_check == 0:
            logger.warning(invoice)
            logger.warning(voucher_id)
            raise osv.except_osv(_("Warning"), _("I did not create any voucher line"))
        elif double_check > 1:
            logger.warning(invoice)
            logger.warning(voucher_id)
            raise osv.except_osv(_("Warning"), _("I created multiple voucher line ??"))


        # Where the magic happen
        voucher_id.signal_workflow("proforma_voucher")
        logger.info('Invoice was mark as paid')
        return voucher_id


class alkivi_sepa_line(models.Model):
    _name = 'alkivi.sepa.line'
    _description = 'SEPA Line'

    sepa_id = fields.Many2one('alkivi.sepa', string='SEPA Report Reference',
            ondelete='cascade', index=True)
    invoice_id = fields.Many2one('account.invoice', string='Invoice Reference',
            ondelete='cascade', index=True)
    partner_id = fields.Many2one('res.partner', string='Partner',
            related='invoice_id.partner_id', store=True, readonly=True)
    amount_total = fields.Float(related='invoice_id.amount_total',
            store=True, readonly=True)

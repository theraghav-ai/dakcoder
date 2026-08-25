package handler

import (
	"regexp"
	"strconv"
	"time"

	"github.com/go-playground/validator/v10"
	//en_translations "github.com/go-playground/validator/v10/translations/en"
)

func ValidatePaocode(fl validator.FieldLevel) bool {
	sixDigitRegex := regexp.MustCompile(`^\d{6}$`)
	return sixDigitRegex.MatchString(fl.Field().String())
}

func ValidateDdocode(fl validator.FieldLevel) bool {
	code := fl.Field().String()
	if len(code) != 6 {
		return false
	}
	for _, ch := range code {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	return true
}

func ValidatePeriod(fl validator.FieldLevel) bool {
	s := fl.Field().String()
	if len(s) != 6 {
		return false
	}

	firstPart := s[:2]
	secondPart := s[2:]

	firstPartInt, err := strconv.Atoi(firstPart)
	if err != nil || firstPartInt < 0 || firstPartInt > 12 {
		return false
	}

	secondPartInt, err := strconv.Atoi(secondPart)
	if err != nil || secondPartInt < 1900 || secondPartInt > 2100 {
		return false
	}

	return true
}
func ValidatedatetimeFormat(fl validator.FieldLevel) bool {
	date := fl.Field().String()
	_, err := time.Parse("2006-01-02T15:04:05Z", date)
	return err == nil
}
